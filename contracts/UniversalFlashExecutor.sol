// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/// @notice Minimal ERC-20 / BEP-20 interface
interface IERC20 {
    function balanceOf(address account) external view returns (uint256);
    function transfer(address recipient, uint256 amount) external returns (bool);
    function approve(address spender, uint256 amount) external returns (bool);
}

/// @notice PancakeSwap V3 / Uniswap V3 Flash Pool interface
interface IPancakeV3FlashPool {
    function flash(
        address recipient,
        uint256 amount0,
        uint256 amount1,
        bytes calldata data
    ) external;
}

/// @notice Aave V3 / Kinza Finance Pool interface (FlashLoanSimple + LiquidationCall)
interface IAaveV3Pool {
    function flashLoanSimple(
        address receiverAddress,
        address asset,
        uint256 amount,
        bytes calldata params,
        uint16 referralCode
    ) external;

    function liquidationCall(
        address collateralAsset,
        address debtAsset,
        address user,
        uint256 debtToCover,
        bool receiveAToken
    ) external;
}

/// @notice Venus Protocol / Compound V2 vToken interface
interface IVToken {
    function liquidateBorrow(
        address borrower,
        uint256 repayAmount,
        address vTokenCollateral
    ) external returns (uint256);

    function redeem(uint256 redeemTokens) external returns (uint256);
}

/// @notice PancakeSwap V3 / Uniswap V3 SwapRouter exactInputSingle interface
interface IDEXSwapRouter {
    struct ExactInputSingleParams {
        address tokenIn;
        address tokenOut;
        uint24 fee;
        address recipient;
        uint256 deadline;
        uint256 amountIn;
        uint256 amountOutMinimum;
        uint160 sqrtPriceLimitX96;
    }

    function exactInputSingle(ExactInputSingleParams calldata params) external payable returns (uint256 amountOut);
}

/// @notice Lista DAO / Liquity Dutch Auction Kick interface
interface IAuctionClipper {
    function kick(address vaultOwner, uint256 debtToKick) external returns (uint256 auctionId);
}

/// @notice Beefy Finance / Yield Vault Harvest interface
interface IYieldVault {
    function harvest() external;
    function harvest(address callFeeRecipient) external;
}

/// @notice Perpetual DEX Order & Liquidation Keeper interface (ApolloX / GMX style)
interface IPerpOrderBook {
    function executeOrder(uint256 orderId, address payable feeReceiver) external;
}

/// @title Sentinel Fleet — UniversalFlashExecutor v2.0 (5-in-1 Atomic On-Chain Commission Executor)
/// @notice Non-custodial, polymorphic atomic executor for:
///   1. Bot #1: Flash Loan Liquidations (Venus Protocol & Kinza/Aave V3)
///   2. Bot #2: Atomic DEX-to-DEX Flash Swap Arbitrage (PancakeSwap V3 <-> V2 / Thena)
///   3. Bot #3: Collateralized Stablecoin Dutch Auction Kick Bounties (Lista DAO)
///   4. Bot #4: Yield Vault Auto-Compounding Harvest Keepers (Beefy Finance)
///   5. Bot #5: Perpetual DEX Limit/Stop-Loss & Liquidation Keepers (ApolloX / GMX)
/// @dev Enforces strict callback authentication, target whitelisting, zero residual allowances,
///      reentrancy lock, and hard atomic revert if final net profit to owner < minNetProfit.
contract UniversalFlashExecutor {
    enum StrategyType {
        NONE,
        BOT1_VENUS_LIQUIDATION,
        BOT1_AAVE_LIQUIDATION,
        BOT2_DEX_ARBITRAGE,
        BOT3_AUCTION_KICK,
        BOT4_VAULT_HARVEST,
        BOT5_PERP_ORDER_EXEC
    }

    struct FlashCallbackContext {
        bool isActive;
        address expectedCaller;
        StrategyType strategy;
        address borrowedToken;
        uint256 borrowedAmount;
    }

    struct FlashExecutionOrder {
        StrategyType strategy;
        address flashPool;
        bool isAaveFlash;
        address borrowedToken;
        uint256 borrowedAmount;
        bool borrowToken0;
        address targetProtocol;
        address borrowerOrVault;
        address collateralToken;
        address vTokenCollateral;
        address dexRouterBuy;
        address dexRouterSell;
        uint24 poolFeeBuy;
        uint24 poolFeeSell;
        uint256 minSwapOut;
        uint256 minNetProfit;
    }

    address public owner;
    address public pendingOwner;
    bool public paused;

    uint256 private _reentrancyStatus;
    uint256 private constant _NOT_entered = 1;
    uint256 private constant _ENTERED = 2;

    FlashCallbackContext private _callbackContext;
    mapping(address => bool) public whitelistedTargets;

    event OwnershipTransferStarted(address indexed previousOwner, address indexed newOwner);
    event OwnershipTransferred(address indexed previousOwner, address indexed newOwner);
    event TargetWhitelistUpdated(address indexed target, bool allowed);
    event EmergencyPauseUpdated(bool isPaused);
    event StrategyExecuted(
        StrategyType indexed strategy,
        address indexed profitToken,
        uint256 borrowedAmount,
        uint256 netProfitPaidToOwner
    );

    error UnauthorizedOwner();
    error ContractPaused();
    error ReentrancyBlocked();
    error InvalidAddress();
    error TargetNotWhitelisted(address target);
    error UnauthorizedCallbackCaller(address caller, address expected);
    error CallbackNotActive();
    error UnauthorizedFlashInitiator(address initiator);
    error VenusLiquidationFailed(uint256 errorCode);
    error VenusRedeemFailed(uint256 errorCode);
    error ERC20TransferFailed(address token, address to, uint256 amount);
    error ERC20ApproveFailed(address token, address spender, uint256 amount);
    error AtomicRevertInsufficientNetProfit(uint256 actualProfit, uint256 requiredMinProfit);

    modifier onlyOwner() {
        if (msg.sender != owner) revert UnauthorizedOwner();
        _;
    }

    modifier whenNotPaused() {
        if (paused) revert ContractPaused();
        _;
    }

    modifier nonReentrant() {
        if (_reentrancyStatus == _ENTERED) revert ReentrancyBlocked();
        _reentrancyStatus = _ENTERED;
        _;
        _reentrancyStatus = _NOT_entered;
    }

    constructor(address initialOwner, address[] memory initialTargets) {
        if (initialOwner == address(0)) revert InvalidAddress();
        owner = initialOwner;
        _reentrancyStatus = _NOT_entered;

        for (uint256 i = 0; i < initialTargets.length; i++) {
            if (initialTargets[i] == address(0)) revert InvalidAddress();
            whitelistedTargets[initialTargets[i]] = true;
            emit TargetWhitelistUpdated(initialTargets[i], true);
        }
    }

    // =========================================================================
    // GOVERNANCE & WHITELIST MANAGEMENT (TWO-STEP OWNERSHIP)
    // =========================================================================

    function transferOwnership(address newOwner) external onlyOwner {
        if (newOwner == address(0)) revert InvalidAddress();
        pendingOwner = newOwner;
        emit OwnershipTransferStarted(owner, newOwner);
    }

    function acceptOwnership() external {
        if (msg.sender != pendingOwner) revert UnauthorizedOwner();
        address oldOwner = owner;
        owner = pendingOwner;
        pendingOwner = address(0);
        emit OwnershipTransferred(oldOwner, owner);
    }

    function setWhitelistedTarget(address target, bool allowed) external onlyOwner {
        if (target == address(0)) revert InvalidAddress();
        whitelistedTargets[target] = allowed;
        emit TargetWhitelistUpdated(target, allowed);
    }

    function setPaused(bool _paused) external onlyOwner {
        paused = _paused;
        emit EmergencyPauseUpdated(_paused);
    }

    // =========================================================================
    // ENTRYPOINT 1: ATOMIC FLASH LOAN STRATEGIES (BOT #1 & BOT #2)
    // =========================================================================

    /// @notice Executes Bot #1 (Flash Liquidation) or Bot #2 (DEX Flash Arbitrage) atomically.
    /// @dev Reverts the entire transaction if final net profit transferred to owner < order.minNetProfit.
    function executeFlashStrategy(FlashExecutionOrder calldata order)
        external
        onlyOwner
        whenNotPaused
        nonReentrant
        returns (uint256 netProfit)
    {
        if (!whitelistedTargets[order.flashPool]) revert TargetNotWhitelisted(order.flashPool);
        if (!whitelistedTargets[order.targetProtocol]) revert TargetNotWhitelisted(order.targetProtocol);
        if (order.borrowedToken == address(0) || order.borrowedAmount == 0) revert InvalidAddress();

        uint256 ownerBalanceBefore = IERC20(order.borrowedToken).balanceOf(owner);

        _callbackContext = FlashCallbackContext({
            isActive: true,
            expectedCaller: order.flashPool,
            strategy: order.strategy,
            borrowedToken: order.borrowedToken,
            borrowedAmount: order.borrowedAmount
        });

        bytes memory encodedOrder = abi.encode(order);

        if (order.isAaveFlash) {
            IAaveV3Pool(order.flashPool).flashLoanSimple(
                address(this),
                order.borrowedToken,
                order.borrowedAmount,
                encodedOrder,
                0
            );
        } else {
            uint256 amount0 = order.borrowToken0 ? order.borrowedAmount : 0;
            uint256 amount1 = order.borrowToken0 ? 0 : order.borrowedAmount;
            IPancakeV3FlashPool(order.flashPool).flash(
                address(this),
                amount0,
                amount1,
                encodedOrder
            );
        }

        delete _callbackContext;

        // Sweep all remaining borrowedToken and collateralToken to owner (maintaining $0.00 custodial balance)
        uint256 contractResidual = IERC20(order.borrowedToken).balanceOf(address(this));
        if (contractResidual > 0) {
            _safeTransfer(order.borrowedToken, owner, contractResidual);
        }
        if (order.collateralToken != address(0) && order.collateralToken != order.borrowedToken) {
            uint256 collateralResidual = IERC20(order.collateralToken).balanceOf(address(this));
            if (collateralResidual > 0) {
                _safeTransfer(order.collateralToken, owner, collateralResidual);
            }
        }

        uint256 ownerBalanceAfter = IERC20(order.borrowedToken).balanceOf(owner);
        netProfit = ownerBalanceAfter > ownerBalanceBefore ? (ownerBalanceAfter - ownerBalanceBefore) : 0;

        if (netProfit < order.minNetProfit) {
            revert AtomicRevertInsufficientNetProfit(netProfit, order.minNetProfit);
        }

        emit StrategyExecuted(order.strategy, order.borrowedToken, order.borrowedAmount, netProfit);
    }

    // =========================================================================
    // FLASH LOAN CALLBACKS (PANCAKESWAP V3 & AAVE V3 / KINZA)
    // =========================================================================

    function pancakeV3FlashCallback(
        uint256 fee0,
        uint256 fee1,
        bytes calldata data
    ) external {
        if (!_callbackContext.isActive) revert CallbackNotActive();
        if (msg.sender != _callbackContext.expectedCaller) {
            revert UnauthorizedCallbackCaller(msg.sender, _callbackContext.expectedCaller);
        }

        FlashExecutionOrder memory order = abi.decode(data, (FlashExecutionOrder));
        uint256 flashFee = order.borrowToken0 ? fee0 : fee1;
        uint256 totalRepayment = order.borrowedAmount + flashFee;

        _dispatchFlashLogic(order);

        // Repay PancakeSwap V3 Flash Loan directly to pool
        _safeTransfer(order.borrowedToken, msg.sender, totalRepayment);
    }

    function executeOperation(
        address asset,
        uint256 amount,
        uint256 premium,
        address initiator,
        bytes calldata params
    ) external returns (bool) {
        if (!_callbackContext.isActive) revert CallbackNotActive();
        if (msg.sender != _callbackContext.expectedCaller) {
            revert UnauthorizedCallbackCaller(msg.sender, _callbackContext.expectedCaller);
        }
        if (initiator != address(this)) revert UnauthorizedFlashInitiator(initiator);

        FlashExecutionOrder memory order = abi.decode(params, (FlashExecutionOrder));
        _dispatchFlashLogic(order);

        // Approve exact repayment (amount + premium) to Aave V3 / Kinza Pool
        uint256 totalRepayment = amount + premium;
        _safeApproveExact(asset, msg.sender, totalRepayment);
        return true;
    }

    function _dispatchFlashLogic(FlashExecutionOrder memory order) internal {
        if (order.strategy == StrategyType.BOT1_VENUS_LIQUIDATION) {
            if (!whitelistedTargets[order.dexRouterSell]) revert TargetNotWhitelisted(order.dexRouterSell);
            if (!whitelistedTargets[order.vTokenCollateral]) revert TargetNotWhitelisted(order.vTokenCollateral);

            // 1. Approve borrowed debt token to Venus vToken and liquidate borrower
            _safeApproveExact(order.borrowedToken, order.targetProtocol, order.borrowedAmount);
            uint256 liqErr = IVToken(order.targetProtocol).liquidateBorrow(
                order.borrowerOrVault,
                order.borrowedAmount,
                order.vTokenCollateral
            );
            if (liqErr != 0) revert VenusLiquidationFailed(liqErr);
            _safeApproveExact(order.borrowedToken, order.targetProtocol, 0);

            // 2. Redeem seized vTokenCollateral into underlying collateral token
            uint256 seizedVTokens = IERC20(order.vTokenCollateral).balanceOf(address(this));
            uint256 redErr = IVToken(order.vTokenCollateral).redeem(seizedVTokens);
            if (redErr != 0) revert VenusRedeemFailed(redErr);

            // 3. Swap underlying collateral back to borrowedToken via DEX Router
            uint256 collateralBal = IERC20(order.collateralToken).balanceOf(address(this));
            _safeApproveExact(order.collateralToken, order.dexRouterSell, collateralBal);
            IDEXSwapRouter(order.dexRouterSell).exactInputSingle(
                IDEXSwapRouter.ExactInputSingleParams({
                    tokenIn: order.collateralToken,
                    tokenOut: order.borrowedToken,
                    fee: order.poolFeeSell,
                    recipient: address(this),
                    deadline: block.timestamp,
                    amountIn: collateralBal,
                    amountOutMinimum: order.minSwapOut,
                    sqrtPriceLimitX96: 0
                })
            );
            _safeApproveExact(order.collateralToken, order.dexRouterSell, 0);
        } else if (order.strategy == StrategyType.BOT1_AAVE_LIQUIDATION) {
            if (!whitelistedTargets[order.dexRouterSell]) revert TargetNotWhitelisted(order.dexRouterSell);

            // 1. Approve debt asset to Aave/Kinza Pool and execute liquidationCall
            _safeApproveExact(order.borrowedToken, order.targetProtocol, order.borrowedAmount);
            IAaveV3Pool(order.targetProtocol).liquidationCall(
                order.collateralToken,
                order.borrowedToken,
                order.borrowerOrVault,
                order.borrowedAmount,
                false
            );
            _safeApproveExact(order.borrowedToken, order.targetProtocol, 0);

            // 2. Swap seized collateral back to borrowedToken
            uint256 seizedCollateral = IERC20(order.collateralToken).balanceOf(address(this));
            _safeApproveExact(order.collateralToken, order.dexRouterSell, seizedCollateral);
            IDEXSwapRouter(order.dexRouterSell).exactInputSingle(
                IDEXSwapRouter.ExactInputSingleParams({
                    tokenIn: order.collateralToken,
                    tokenOut: order.borrowedToken,
                    fee: order.poolFeeSell,
                    recipient: address(this),
                    deadline: block.timestamp,
                    amountIn: seizedCollateral,
                    amountOutMinimum: order.minSwapOut,
                    sqrtPriceLimitX96: 0
                })
            );
            _safeApproveExact(order.collateralToken, order.dexRouterSell, 0);
        } else if (order.strategy == StrategyType.BOT2_DEX_ARBITRAGE) {
            if (!whitelistedTargets[order.dexRouterBuy]) revert TargetNotWhitelisted(order.dexRouterBuy);
            if (!whitelistedTargets[order.dexRouterSell]) revert TargetNotWhitelisted(order.dexRouterSell);

            // Leg 1: Buy target token on Pool A (dexRouterBuy)
            _safeApproveExact(order.borrowedToken, order.dexRouterBuy, order.borrowedAmount);
            uint256 boughtAmount = IDEXSwapRouter(order.dexRouterBuy).exactInputSingle(
                IDEXSwapRouter.ExactInputSingleParams({
                    tokenIn: order.borrowedToken,
                    tokenOut: order.collateralToken,
                    fee: order.poolFeeBuy,
                    recipient: address(this),
                    deadline: block.timestamp,
                    amountIn: order.borrowedAmount,
                    amountOutMinimum: 1,
                    sqrtPriceLimitX96: 0
                })
            );
            _safeApproveExact(order.borrowedToken, order.dexRouterBuy, 0);

            // Leg 2: Sell target token on Pool B (dexRouterSell)
            _safeApproveExact(order.collateralToken, order.dexRouterSell, boughtAmount);
            IDEXSwapRouter(order.dexRouterSell).exactInputSingle(
                IDEXSwapRouter.ExactInputSingleParams({
                    tokenIn: order.collateralToken,
                    tokenOut: order.borrowedToken,
                    fee: order.poolFeeSell,
                    recipient: address(this),
                    deadline: block.timestamp,
                    amountIn: boughtAmount,
                    amountOutMinimum: order.minSwapOut,
                    sqrtPriceLimitX96: 0
                })
            );
            _safeApproveExact(order.collateralToken, order.dexRouterSell, 0);
        }
    }

    // =========================================================================
    // ENTRYPOINT 2: ZERO-CAPITAL KEEPER STRATEGIES (BOT #3, BOT #4, BOT #5)
    // =========================================================================

    /// @notice Executes Bot #3 (Auction Kick), Bot #4 (Vault Harvest), or Bot #5 (Perp Order Keeper)
    ///         and enforces that net reward transferred to owner >= minNetProfit.
    function executeKeeperStrategy(
        StrategyType strategy,
        address targetContract,
        address rewardToken,
        address vaultOwnerForKick,
        uint256 debtOrOrderId,
        uint256 minNetProfit
    ) external onlyOwner whenNotPaused nonReentrant returns (uint256 netProfit) {
        if (!whitelistedTargets[targetContract]) revert TargetNotWhitelisted(targetContract);
        if (rewardToken == address(0)) revert InvalidAddress();

        uint256 ownerBalBefore = IERC20(rewardToken).balanceOf(owner);

        if (strategy == StrategyType.BOT3_AUCTION_KICK) {
            IAuctionClipper(targetContract).kick(vaultOwnerForKick, debtOrOrderId);
        } else if (strategy == StrategyType.BOT4_VAULT_HARVEST) {
            IYieldVault(targetContract).harvest(owner);
        } else if (strategy == StrategyType.BOT5_PERP_ORDER_EXEC) {
            IPerpOrderBook(targetContract).executeOrder(debtOrOrderId, payable(owner));
        } else {
            revert InvalidAddress();
        }

        uint256 contractReward = IERC20(rewardToken).balanceOf(address(this));
        if (contractReward > 0) {
            _safeTransfer(rewardToken, owner, contractReward);
        }

        uint256 ownerBalAfter = IERC20(rewardToken).balanceOf(owner);
        netProfit = ownerBalAfter > ownerBalBefore ? (ownerBalAfter - ownerBalBefore) : 0;

        if (netProfit < minNetProfit) {
            revert AtomicRevertInsufficientNetProfit(netProfit, minNetProfit);
        }

        emit StrategyExecuted(strategy, rewardToken, 0, netProfit);
    }

    // =========================================================================
    // EMERGENCY TOKEN RESCUE & INTERNAL SAFE ERC20 HELPERS
    // =========================================================================

    function rescueTokens(address token, uint256 amount) external onlyOwner nonReentrant {
        _safeTransfer(token, owner, amount);
    }

    function _safeTransfer(address token, address to, uint256 value) internal {
        (bool success, bytes memory data) = token.call(
            abi.encodeWithSelector(IERC20.transfer.selector, to, value)
        );
        if (!success || (data.length != 0 && !abi.decode(data, (bool)))) {
            revert ERC20TransferFailed(token, to, value);
        }
    }

    function _safeApproveExact(address token, address spender, uint256 value) internal {
        if (value > 0) {
            // Reset to 0 first (USDT / BEP-20 safe pattern)
            (bool s0, bytes memory d0) = token.call(
                abi.encodeWithSelector(IERC20.approve.selector, spender, 0)
            );
            if (!s0 || (d0.length != 0 && !abi.decode(d0, (bool)))) {
                revert ERC20ApproveFailed(token, spender, 0);
            }
        }
        (bool success, bytes memory data) = token.call(
            abi.encodeWithSelector(IERC20.approve.selector, spender, value)
        );
        if (!success || (data.length != 0 && !abi.decode(data, (bool)))) {
            revert ERC20ApproveFailed(token, spender, value);
        }
    }
}
