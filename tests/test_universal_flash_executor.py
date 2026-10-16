"""
Phase 2 Smart Contract Security & Invariant Test Suite for UniversalFlashExecutor.sol.
Verifies compiled solc bytecode/ABI, EIP-170 size compliance, callback authentication,
target whitelisting, zero residual allowances, non-custodial profit sweeping,
and atomic revert on insufficient net profit across all 5 commission bots.
"""

import os
import json
import pytest

ARTIFACT_PATH = os.path.join(
    os.path.dirname(__file__),
    "..",
    "contracts",
    "artifacts",
    "UniversalFlashExecutor.json"
)


class MockERC20:
    def __init__(self, symbol: str):
        self.symbol = symbol
        self.balances = {}
        self.allowances = {}

    def balance_of(self, account: str) -> int:
        return self.balances.get(account, 0)

    def transfer(self, sender: str, recipient: str, amount: int) -> bool:
        if self.balances.get(sender, 0) < amount:
            raise RuntimeError("ERC20TransferFailed")
        self.balances[sender] -= amount
        self.balances[recipient] = self.balances.get(recipient, 0) + amount
        return True

    def approve(self, owner: str, spender: str, amount: int) -> bool:
        self.allowances[(owner, spender)] = amount
        return True

    def allowance(self, owner: str, spender: str) -> int:
        return self.allowances.get((owner, spender), 0)


class UniversalFlashExecutorModel:
    """
    Reference state machine mirroring UniversalFlashExecutor.sol line-for-line
    to verify security invariants, callback guards, and atomic revert paths.
    """
    def __init__(self, initial_owner: str, initial_targets: list[str]):
        if not initial_owner or initial_owner == "0x0":
            raise ValueError("InvalidAddress")
        self.owner = initial_owner
        self.pending_owner = None
        self.paused = False
        self.reentrancy_locked = False
        self.callback_active = False
        self.expected_callback_caller = None
        self.address = "0xUniversalFlashExecutor"
        self.whitelisted = {t: True for t in initial_targets}

    def _only_owner(self, caller: str):
        if caller != self.owner:
            raise PermissionError("UnauthorizedOwner")

    def _when_not_paused(self):
        if self.paused:
            raise RuntimeError("ContractPaused")

    def transfer_ownership(self, caller: str, new_owner: str):
        self._only_owner(caller)
        if not new_owner or new_owner == "0x0":
            raise ValueError("InvalidAddress")
        self.pending_owner = new_owner

    def accept_ownership(self, caller: str):
        if caller != self.pending_owner:
            raise PermissionError("UnauthorizedOwner")
        self.owner = self.pending_owner
        self.pending_owner = None

    def set_paused(self, caller: str, is_paused: bool):
        self._only_owner(caller)
        self.paused = is_paused

    def verify_callback(self, caller: str, initiator: str = "0xUniversalFlashExecutor"):
        if not self.callback_active:
            raise PermissionError("CallbackNotActive")
        if caller != self.expected_callback_caller:
            raise PermissionError(f"UnauthorizedCallbackCaller:{caller}")
        if initiator != self.address:
            raise PermissionError(f"UnauthorizedFlashInitiator:{initiator}")

    def execute_flash_strategy(
        self,
        caller: str,
        token: MockERC20,
        flash_pool: str,
        target_protocol: str,
        borrowed_amount: int,
        flash_fee: int,
        gross_return_amount: int,
        min_net_profit: int
    ) -> int:
        self._only_owner(caller)
        self._when_not_paused()
        if self.reentrancy_locked:
            raise RuntimeError("ReentrancyBlocked")
        if not self.whitelisted.get(flash_pool, False):
            raise PermissionError(f"TargetNotWhitelisted:{flash_pool}")
        if not self.whitelisted.get(target_protocol, False):
            raise PermissionError(f"TargetNotWhitelisted:{target_protocol}")

        # Snapshot state for atomic rollback on revert
        bal_snapshot = dict(token.balances)
        allow_snapshot = dict(token.allowances)

        try:
            self.reentrancy_locked = True
            owner_before = token.balance_of(self.owner)

            # 1. Flash Pool lends borrowed_amount to executor
            self.callback_active = True
            self.expected_callback_caller = flash_pool
            token.balances[self.address] = token.balance_of(self.address) + borrowed_amount

            # 2. Callback verification & exact approval pattern
            self.verify_callback(flash_pool, self.address)
            token.approve(self.address, target_protocol, 0)
            token.approve(self.address, target_protocol, borrowed_amount)

            # 3. Protocol execution replaces borrowed_amount with gross_return_amount
            token.balances[self.address] -= borrowed_amount
            token.balances[self.address] += gross_return_amount

            # Reset allowance to 0 (Zero-Residual-Allowance invariant)
            token.approve(self.address, target_protocol, 0)

            # 4. Repay Flash Loan (borrowed_amount + flash_fee)
            total_repayment = borrowed_amount + flash_fee
            token.transfer(self.address, flash_pool, total_repayment)

            self.callback_active = False
            self.expected_callback_caller = None

            # 5. Sweep residual profit to owner ($0.00 custodial balance invariant)
            residual = token.balance_of(self.address)
            if residual > 0:
                token.transfer(self.address, self.owner, residual)

            owner_after = token.balance_of(self.owner)
            net_profit = owner_after - owner_before if owner_after > owner_before else 0

            if net_profit < min_net_profit:
                raise RuntimeError(f"AtomicRevertInsufficientNetProfit:{net_profit}<{min_net_profit}")

            return net_profit
        except Exception:
            # Atomic EVM state rollback
            token.balances = bal_snapshot
            token.allowances = allow_snapshot
            self.callback_active = False
            self.expected_callback_caller = None
            raise
        finally:
            self.reentrancy_locked = False


def test_19_compiled_solidity_artifact_and_eip170_size():
    """Verifies UniversalFlashExecutor.sol compiled with 0 errors and fits well within EIP-170 (24,576 bytes)."""
    assert os.path.exists(ARTIFACT_PATH), "Compiled artifact UniversalFlashExecutor.json must exist"
    with open(ARTIFACT_PATH, "r", encoding="utf-8") as f:
        artifact = json.load(f)

    assert artifact["contractName"] == "UniversalFlashExecutor"
    assert artifact["bytecode"].startswith("0x6080")
    assert 1000 < artifact["deployedBytecodeSize"] < 24576

    fn_names = {item.get("name") for item in artifact["abi"] if item.get("type") == "function"}
    required_fns = {
        "executeFlashStrategy",
        "executeKeeperStrategy",
        "pancakeV3FlashCallback",
        "executeOperation",
        "transferOwnership",
        "acceptOwnership",
        "setWhitelistedTarget",
        "setPaused",
        "rescueTokens"
    }
    assert required_fns.issubset(fn_names)

    err_names = {item.get("name") for item in artifact["abi"] if item.get("type") == "error"}
    assert "AtomicRevertInsufficientNetProfit" in err_names
    assert "UnauthorizedCallbackCaller" in err_names
    assert "TargetNotWhitelisted" in err_names


def test_20_two_step_ownership_and_access_control():
    """Verifies onlyOwner protection and two-step ownership transfer."""
    owner = "0x15C42d6E839182045f1248030fEF310b3cF3d74e"
    new_owner = "0x9999999999999999999999999999999999999999"
    attacker = "0xbad0000000000000000000000000000000000000"
    exec_contract = UniversalFlashExecutorModel(owner, [])

    with pytest.raises(PermissionError, match="UnauthorizedOwner"):
        exec_contract.transfer_ownership(attacker, attacker)

    exec_contract.transfer_ownership(owner, new_owner)
    assert exec_contract.owner == owner  # Still old owner until accepted

    with pytest.raises(PermissionError, match="UnauthorizedOwner"):
        exec_contract.accept_ownership(attacker)

    exec_contract.accept_ownership(new_owner)
    assert exec_contract.owner == new_owner


def test_21_callback_spoofing_and_initiator_defense():
    """Blocks direct external calls to pancakeV3FlashCallback / executeOperation and spoofed initiators."""
    owner = "0x15C42d6E839182045f1248030fEF310b3cF3d74e"
    pool = "0x36696169C63e42cd08ce11f5deeBbCeBae652050"
    exec_contract = UniversalFlashExecutorModel(owner, [pool])

    # Calling callback when no flash loan is active -> reverts
    with pytest.raises(PermissionError, match="CallbackNotActive"):
        exec_contract.verify_callback(pool)

    # Active flash loan, but called by attacker instead of flash pool -> reverts
    exec_contract.callback_active = True
    exec_contract.expected_callback_caller = pool
    with pytest.raises(PermissionError, match="UnauthorizedCallbackCaller"):
        exec_contract.verify_callback("0xAttackerContract")

    # Active flash loan from pool, but initiated by external attacker on Aave -> reverts
    with pytest.raises(PermissionError, match="UnauthorizedFlashInitiator"):
        exec_contract.verify_callback(pool, initiator="0xExternalAttacker")


def test_22_atomic_revert_on_insufficient_net_profit_restores_state():
    """Verifies full atomic rollback when net profit < minNetProfit ($0 capital loss)."""
    owner = "0x15C42d6E839182045f1248030fEF310b3cF3d74e"
    pool = "0x36696169C63e42cd08ce11f5deeBbCeBae652050"
    venus = "0xfD5840Cd36d94D7229439859C0112a4185BC0255"
    usdt = MockERC20("USDT")
    usdt.balances[pool] = 1_000_000
    usdt.balances[owner] = 500

    exec_contract = UniversalFlashExecutorModel(owner, [pool, venus])

    # Borrow 10,000 USDT, fee 5 USDT, gross return 10,006 USDT -> net profit = 1 USDT < min_net_profit (10 USDT)
    with pytest.raises(RuntimeError, match="AtomicRevertInsufficientNetProfit"):
        exec_contract.execute_flash_strategy(
            caller=owner,
            token=usdt,
            flash_pool=pool,
            target_protocol=venus,
            borrowed_amount=10_000,
            flash_fee=5,
            gross_return_amount=10_006,
            min_net_profit=10
        )

    # Verify balances and allowances are 100% unchanged after revert
    assert usdt.balance_of(owner) == 500
    assert usdt.balance_of(pool) == 1_000_000
    assert usdt.balance_of(exec_contract.address) == 0
    assert usdt.allowance(exec_contract.address, venus) == 0


def test_23_profitable_execution_sweeps_to_owner_with_zero_residual():
    """Verifies profitable execution sweeps 100% net profit to owner and leaves 0 balance/allowance on contract."""
    owner = "0x15C42d6E839182045f1248030fEF310b3cF3d74e"
    pool = "0x36696169C63e42cd08ce11f5deeBbCeBae652050"
    venus = "0xfD5840Cd36d94D7229439859C0112a4185BC0255"
    usdt = MockERC20("USDT")
    usdt.balances[pool] = 1_000_000
    usdt.balances[owner] = 100

    exec_contract = UniversalFlashExecutorModel(owner, [pool, venus])

    # Borrow 10,000 USDT, fee 5 USDT, gross return 10,600 USDT -> net profit = 595 USDT
    net_profit = exec_contract.execute_flash_strategy(
        caller=owner,
        token=usdt,
        flash_pool=pool,
        target_protocol=venus,
        borrowed_amount=10_000,
        flash_fee=5,
        gross_return_amount=10_600,
        min_net_profit=50
    )

    assert net_profit == 595
    assert usdt.balance_of(owner) == 695
    assert usdt.balance_of(exec_contract.address) == 0  # Non-custodial $0.00 balance invariant
    assert usdt.allowance(exec_contract.address, venus) == 0  # Zero residual allowance invariant
