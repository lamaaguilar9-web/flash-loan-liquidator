"""
Flash Loan Liquidator (Module C) - Unified 5-in-1 On-Chain Commission Engine.

Official Live Operational Scope (M3 Policy):
  - 2 Live Economic Evaluators (Lending Liquidations on Venus/Kinza/Aave + DEX Flash Arbitrage)
  - 3 Live On-Chain Monitors (Lista DAO lisUSD, Beefy Finance Vaults, GMX Arbitrum Vault)
  - Plus mathematical PnL evaluators for all 5 strategies inside a single memory space:
    1. Bot #1 (STRATEGY_1_FLASH_LIQUIDATION): Lending liquidations with Flash Loans (Venus/Kinza/Aave)
    2. Bot #2 (STRATEGY_2_DEX_FLASH_ARBITRAGE): Atomic DEX-to-DEX Flash Swap arbitrage (PancakeSwap V3 <-> V2)
    3. Bot #3 (STRATEGY_3_AUCTION_KICK_KEEPER): Collateralized stablecoin Dutch Auction kick() keeper (Lista DAO)
    4. Bot #4 (STRATEGY_4_VAULT_HARVEST_KEEPER): Auto-compounding yield vault harvest() callFee keeper (Beefy)
    5. Bot #5 (STRATEGY_5_PERP_ORDER_AND_LIQ_KEEPER): Perpetual DEX limit/stop-loss & margin liquidation keeper (GMX/ApolloX)
"""

from typing import Dict, Any, List, Optional

# Canonical EVM Function Selectors
SELECTOR_GET_RESERVES_V2 = "0x0902f1ac"   # getReserves() -> (uint112 reserve0, uint112 reserve1, uint32 ts)
SELECTOR_HARVEST = "0x4641257d"           # harvest()
SELECTOR_CALL_REWARD = "0x97fd323d"       # callReward() -> uint256
SELECTOR_VAULT_BALANCE = "0xb69ef8a8"     # balance() -> uint256
SELECTOR_AUCTION_KICK = "0x0203d8fb"      # kick(address,uint256)
SELECTOR_EXECUTE_ORDER = "0x7e498c81"     # executeOrder(uint256,address)


def decode_uniswap_v2_reserves(
    raw_hex: str,
    decimals0: int = 18,
    decimals1: int = 18,
    invert_price: bool = False
) -> Dict[str, Any]:
    """
    Decodes PancakeSwap V2 / Uniswap V2 getReserves() response.

    Uniswap V2 Token Ordering Rule (M1):
      In Uniswap V2 / PancakeSwap V2 pairs, token0 and token1 are sorted immutably by address
      (token0 < token1).
      - For WBNB/USDT (0x16b9a828...): USDT (0x55d398...) < WBNB (0xbb4CdB...), so token0=USDT
        and token1=WBNB. Thus reserve0/reserve1 yields USD per WBNB (invert_price=False).
      - If a future pool has base asset < quote asset (e.g., BTCB 0x7130d2... < USDT 0x55d398...),
        token0=BTCB and token1=USDT, so invert_price=True must be set in config/markets.json
        to compute reserve1/reserve0.
    """
    if not raw_hex or not isinstance(raw_hex, str) or not raw_hex.startswith("0x") or len(raw_hex) < 130:
        return {"is_valid": False, "reason": "INVALID_V2_RESERVES_HEX"}

    payload = raw_hex[2:]
    try:
        r0_raw = int(payload[0:64], 16)
        r1_raw = int(payload[64:128], 16)
    except ValueError:
        return {"is_valid": False, "reason": "V2_HEX_PARSE_ERROR"}

    if r0_raw <= 0 or r1_raw <= 0:
        return {"is_valid": False, "reason": "ZERO_V2_RESERVES"}

    reserve0 = r0_raw / (10 ** decimals0)
    reserve1 = r1_raw / (10 ** decimals1)
    spot_price = (reserve1 / reserve0) if invert_price else (reserve0 / reserve1)

    return {
        "is_valid": True,
        "reserve0": round(reserve0, 4),
        "reserve1": round(reserve1, 4),
        "invert_price": invert_price,
        "spot_price_usd": round(spot_price, 6)
    }


class UnifiedCommissionEngine:
    """
    Unified 5-in-1 On-Chain Commission Engine:
    Evaluates all 5 commission strategies in memory using shared RPC & Chainlink state,
    and ranks all actionable opportunities in a single Profit Priority Queue.
    """

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.min_net_profit_usd = float(config.get("min_net_profit_usd", 1.50))

    # ==========================================================================
    # BOT #2: ARBITRAJE ATÓMICO DEX-A-DEX CON FLASH SWAPS
    # ==========================================================================
    def evaluate_dex_flash_arbitrage(
        self,
        pair_symbol: str,
        chain: str,
        pool_a_name: str,
        pool_a_price_usd: Optional[float],
        pool_a_fee_rate: float,
        pool_b_name: str,
        pool_b_price_usd: Optional[float],
        pool_b_fee_rate: float,
        trade_notional_usd: float,
        native_token_price_usd: Optional[float],
        gas_price_gwei: Optional[float],
        oracle_is_valid: bool = True,
        gas_limit: int = 280000
    ) -> Dict[str, Any]:
        """
        Bot #2: Simulates an atomic Flash Swap arbitrage between Pool A and Pool B in one block.
        """
        if (
            not oracle_is_valid
            or pool_a_price_usd is None or pool_a_price_usd <= 0.0
            or pool_b_price_usd is None or pool_b_price_usd <= 0.0
            or native_token_price_usd is None or native_token_price_usd <= 0.0
            or gas_price_gwei is None or gas_price_gwei <= 0.0
            or trade_notional_usd <= 0.0
        ):
            return {
                "strategy_id": "BOT_2_DEX_FLASH_ARBITRAGE",
                "strategy_type": "DEX_FLASH_SWAP_ARBITRAGE",
                "status": "ABORTED_INVALID_TELEMETRY",
                "would_execute_atomic": False,
                "chain": chain,
                "pair": pair_symbol,
                "net_profit_usd": 0.0
            }

        low_price = min(pool_a_price_usd, pool_b_price_usd)
        high_price = max(pool_a_price_usd, pool_b_price_usd)
        buy_pool = pool_a_name if pool_a_price_usd <= pool_b_price_usd else pool_b_name
        sell_pool = pool_b_name if buy_pool == pool_a_name else pool_a_name

        spread_ratio = (high_price - low_price) / low_price
        gross_spread_usd = trade_notional_usd * spread_ratio

        # Both pool swap fees + price impact slippage (8 bps) + on-chain gas
        total_fee_rate = pool_a_fee_rate + pool_b_fee_rate + 0.0008
        dex_fees_and_slippage_usd = trade_notional_usd * total_fee_rate
        gas_cost_usd = ((gas_limit * gas_price_gwei) / 1e9) * native_token_price_usd

        net_profit_usd = round(gross_spread_usd - dex_fees_and_slippage_usd - gas_cost_usd, 4)
        would_execute = net_profit_usd >= self.min_net_profit_usd

        return {
            "strategy_id": "BOT_2_DEX_FLASH_ARBITRAGE",
            "strategy_type": "DEX_FLASH_SWAP_ARBITRAGE",
            "status": "PROFITABLE_OPPORTUNITY_LOGGED" if would_execute else "REVERTED_SPREAD_BELOW_COSTS",
            "would_execute_atomic": would_execute,
            "chain": chain,
            "pair": pair_symbol,
            "buy_pool": buy_pool,
            "sell_pool": sell_pool,
            "buy_price_usd": round(low_price, 4),
            "sell_price_usd": round(high_price, 4),
            "spread_bps": round(spread_ratio * 10000, 2),
            "trade_notional_usd": round(trade_notional_usd, 2),
            "gross_spread_usd": round(gross_spread_usd, 4),
            "dex_fees_and_slippage_usd": round(dex_fees_and_slippage_usd, 4),
            "gas_cost_usd": round(gas_cost_usd, 4),
            "net_profit_usd": net_profit_usd
        }

    # ==========================================================================
    # BOT #3: BOUNTIES DE "KICK" DE SUBASTAS (LISTA DAO / LIQUITY FORKS)
    # ==========================================================================
    def evaluate_auction_kick_bounty(
        self,
        protocol: str,
        chain: str,
        vault_owner: str,
        collateral_usd: float,
        debt_usd: float,
        min_collateral_ratio: float,
        flat_kick_tip_usd: float,
        percentage_kick_bonus_rate: float,
        native_token_price_usd: Optional[float],
        gas_price_gwei: Optional[float],
        oracle_is_valid: bool = True,
        gas_limit: int = 260000
    ) -> Dict[str, Any]:
        """
        Bot #3: Evaluates initiating a Dutch Auction liquidation (`kick`) on an undercollateralized
        CDP vault (e.g. Lista DAO lisUSD on BNB Chain), earning flat tip + kick percentage incentive.
        """
        if (
            not oracle_is_valid
            or native_token_price_usd is None or native_token_price_usd <= 0.0
            or gas_price_gwei is None or gas_price_gwei <= 0.0
            or debt_usd <= 0.0
            or min_collateral_ratio <= 1.0
        ):
            return {
                "strategy_id": "BOT_3_AUCTION_KICK_KEEPER",
                "strategy_type": "AUCTION_KICK_BOUNTY",
                "status": "ABORTED_INVALID_TELEMETRY",
                "would_execute_atomic": False,
                "chain": chain,
                "protocol": protocol,
                "vault_owner": vault_owner,
                "net_profit_usd": 0.0
            }

        current_icr = collateral_usd / debt_usd
        if current_icr >= min_collateral_ratio:
            return {
                "strategy_id": "BOT_3_AUCTION_KICK_KEEPER",
                "strategy_type": "AUCTION_KICK_BOUNTY",
                "status": "SKIPPED_VAULT_ABOVE_MCR",
                "would_execute_atomic": False,
                "chain": chain,
                "protocol": protocol,
                "vault_owner": vault_owner,
                "collateral_ratio": round(current_icr, 4),
                "min_collateral_ratio": min_collateral_ratio,
                "net_profit_usd": 0.0
            }

        gross_kick_bounty_usd = flat_kick_tip_usd + (debt_usd * percentage_kick_bonus_rate)
        gas_cost_usd = ((gas_limit * gas_price_gwei) / 1e9) * native_token_price_usd
        net_profit_usd = round(gross_kick_bounty_usd - gas_cost_usd, 4)
        would_execute = net_profit_usd >= self.min_net_profit_usd

        return {
            "strategy_id": "BOT_3_AUCTION_KICK_KEEPER",
            "strategy_type": "AUCTION_KICK_BOUNTY",
            "status": "PROFITABLE_OPPORTUNITY_LOGGED" if would_execute else "SKIPPED_BOUNTY_BELOW_THRESHOLD",
            "would_execute_atomic": would_execute,
            "chain": chain,
            "protocol": protocol,
            "vault_owner": vault_owner,
            "collateral_ratio": round(current_icr, 4),
            "min_collateral_ratio": min_collateral_ratio,
            "debt_usd": round(debt_usd, 2),
            "flat_kick_tip_usd": round(flat_kick_tip_usd, 4),
            "gross_kick_bounty_usd": round(gross_kick_bounty_usd, 4),
            "gas_cost_usd": round(gas_cost_usd, 4),
            "net_profit_usd": net_profit_usd
        }

    # ==========================================================================
    # BOT #4: COSECHADORES DE BÓVEDAS (HARVEST KEEPERS — BEEFY FINANCE)
    # ==========================================================================
    def evaluate_vault_harvest_keeper(
        self,
        vault_id: str,
        chain: str,
        pending_rewards_usd: float,
        call_fee_rate: float,
        native_token_price_usd: Optional[float],
        gas_price_gwei: Optional[float],
        oracle_is_valid: bool = True,
        gas_limit: int = 220000
    ) -> Dict[str, Any]:
        """
        Bot #4: Evaluates calling public harvest() (selector 0x4641257d) on yield vaults
        (e.g. Beefy Finance on BNB Chain / Arbitrum) to earn the immediate callFee reward.
        """
        if (
            not oracle_is_valid
            or native_token_price_usd is None or native_token_price_usd <= 0.0
            or gas_price_gwei is None or gas_price_gwei <= 0.0
            or pending_rewards_usd < 0.0
            or call_fee_rate <= 0.0
        ):
            return {
                "strategy_id": "BOT_4_VAULT_HARVEST_KEEPER",
                "strategy_type": "VAULT_HARVEST_KEEPER",
                "status": "ABORTED_INVALID_TELEMETRY",
                "would_execute_atomic": False,
                "chain": chain,
                "vault_id": vault_id,
                "net_profit_usd": 0.0
            }

        gross_call_fee_usd = pending_rewards_usd * call_fee_rate
        gas_cost_usd = ((gas_limit * gas_price_gwei) / 1e9) * native_token_price_usd
        net_profit_usd = round(gross_call_fee_usd - gas_cost_usd, 4)
        would_execute = net_profit_usd >= self.min_net_profit_usd

        return {
            "strategy_id": "BOT_4_VAULT_HARVEST_KEEPER",
            "strategy_type": "VAULT_HARVEST_KEEPER",
            "status": "PROFITABLE_OPPORTUNITY_LOGGED" if would_execute else "SKIPPED_HARVEST_BELOW_THRESHOLD",
            "would_execute_atomic": would_execute,
            "chain": chain,
            "vault_id": vault_id,
            "pending_rewards_usd": round(pending_rewards_usd, 4),
            "call_fee_bps": round(call_fee_rate * 10000, 2),
            "gross_call_fee_usd": round(gross_call_fee_usd, 4),
            "gas_cost_usd": round(gas_cost_usd, 4),
            "net_profit_usd": net_profit_usd
        }

    # ==========================================================================
    # BOT #5: KEEPERS DE ÓRDENES LÍMITE Y PERPETUOS (GMX / GAINS / APOLLOX)
    # ==========================================================================
    def evaluate_perp_order_keeper(
        self,
        protocol: str,
        chain: str,
        order_id: str,
        order_type: str,
        direction: str,
        trigger_price_usd: float,
        current_oracle_price_usd: Optional[float],
        position_size_usd: float,
        execution_fee_usd: float,
        keeper_bonus_rate: float,
        native_token_price_usd: Optional[float],
        gas_price_gwei: Optional[float],
        oracle_is_valid: bool = True,
        gas_limit: int = 310000
    ) -> Dict[str, Any]:
        """
        Bot #5: Evaluates triggering a pending limit/stop-loss order or margin liquidation
        on a Perpetual DEX (GMX v2 / ApolloX / Gains Network) when the Chainlink oracle price
        crosses the trigger price.
        """
        if (
            not oracle_is_valid
            or current_oracle_price_usd is None or current_oracle_price_usd <= 0.0
            or native_token_price_usd is None or native_token_price_usd <= 0.0
            or gas_price_gwei is None or gas_price_gwei <= 0.0
            or trigger_price_usd <= 0.0
            or position_size_usd <= 0.0
        ):
            return {
                "strategy_id": "BOT_5_PERP_ORDER_KEEPER",
                "strategy_type": "PERP_ORDER_AND_LIQ_KEEPER",
                "status": "ABORTED_INVALID_TELEMETRY",
                "would_execute_atomic": False,
                "chain": chain,
                "protocol": protocol,
                "order_id": order_id,
                "net_profit_usd": 0.0
            }

        # Determine if trigger condition is met based on direction and order_type
        # STOP_LOSS / LIQUIDATION on LONG triggers when current_price <= trigger_price
        # STOP_LOSS / LIQUIDATION on SHORT triggers when current_price >= trigger_price
        # TAKE_PROFIT on LONG triggers when current_price >= trigger_price
        dir_upper = direction.upper()
        otype_upper = order_type.upper()

        if otype_upper in ("STOP_LOSS", "LIQUIDATION", "LIMIT_BUY"):
            is_triggered = (
                (current_oracle_price_usd <= trigger_price_usd)
                if dir_upper == "LONG"
                else (current_oracle_price_usd >= trigger_price_usd)
            )
        else:  # TAKE_PROFIT / LIMIT_SELL
            is_triggered = (
                (current_oracle_price_usd >= trigger_price_usd)
                if dir_upper == "LONG"
                else (current_oracle_price_usd <= trigger_price_usd)
            )

        if not is_triggered:
            return {
                "strategy_id": "BOT_5_PERP_ORDER_KEEPER",
                "strategy_type": "PERP_ORDER_AND_LIQ_KEEPER",
                "status": "SKIPPED_TRIGGER_PRICE_NOT_REACHED",
                "would_execute_atomic": False,
                "chain": chain,
                "protocol": protocol,
                "order_id": order_id,
                "current_oracle_price_usd": round(current_oracle_price_usd, 4),
                "trigger_price_usd": round(trigger_price_usd, 4),
                "net_profit_usd": 0.0
            }

        gross_keeper_fee_usd = execution_fee_usd + (position_size_usd * keeper_bonus_rate)
        gas_cost_usd = ((gas_limit * gas_price_gwei) / 1e9) * native_token_price_usd
        net_profit_usd = round(gross_keeper_fee_usd - gas_cost_usd, 4)
        would_execute = net_profit_usd >= self.min_net_profit_usd

        return {
            "strategy_id": "BOT_5_PERP_ORDER_KEEPER",
            "strategy_type": "PERP_ORDER_AND_LIQ_KEEPER",
            "status": "PROFITABLE_OPPORTUNITY_LOGGED" if would_execute else "SKIPPED_FEE_BELOW_THRESHOLD",
            "would_execute_atomic": would_execute,
            "chain": chain,
            "protocol": protocol,
            "order_id": order_id,
            "order_type": otype_upper,
            "direction": dir_upper,
            "current_oracle_price_usd": round(current_oracle_price_usd, 4),
            "trigger_price_usd": round(trigger_price_usd, 4),
            "position_size_usd": round(position_size_usd, 2),
            "gross_keeper_fee_usd": round(gross_keeper_fee_usd, 4),
            "gas_cost_usd": round(gas_cost_usd, 4),
            "net_profit_usd": net_profit_usd
        }

    # ==========================================================================
    # COLA CENTRAL DE PRIORIDAD POR GANANCIA NETA (SIN COLISIÓN DE NONCE NI RAM)
    # ==========================================================================
    @staticmethod
    def rank_unified_opportunities(opportunities: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Central Profit Priority Queue across all 5 strategies:
        Filters only executable profitable opportunities across Bots #1, #2, #3, #4, and #5,
        and orders them from highest to lowest net_profit_usd.
        """
        executable = [
            opp for opp in opportunities
            if opp.get("would_execute_atomic") is True and float(opp.get("net_profit_usd", 0.0)) > 0.0
        ]
        return sorted(executable, key=lambda x: float(x.get("net_profit_usd", 0.0)), reverse=True)
