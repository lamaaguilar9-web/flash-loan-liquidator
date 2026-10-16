"""
Flash Loan Liquidator (Module C) - Unified Single-Engine Multi-Strategy Module.

Combines three on-chain commission strategies inside a single memory space and event loop
(sharing the same RPC session, block state, and gas/oracle telemetry):
  1. Strategy A (LIQUIDATION_FLASH_LOAN): Lending protocol liquidations (5%-10% bonus)
  2. Strategy B (DEX_FLASH_SWAP_ARBITRAGE): Atomic DEX-to-DEX spread arbitrage (PancakeSwap V3 <-> V2)
  3. Strategy C (KEEPER_HARVEST_AND_KICK): Public vault harvest() & auction kick() callFee keeper
Includes a central Profit Priority Queue so the single engine ranks all opportunities by
highest net_profit_usd without RAM contention or wallet nonce collisions.
"""

from typing import Dict, Any, List, Optional

SELECTOR_GET_RESERVES_V2 = "0x0902f1ac"  # getReserves() -> (uint112 reserve0, uint112 reserve1, uint32 blockTimestampLast)


def decode_uniswap_v2_reserves(
    raw_hex: str,
    decimals0: int = 18,
    decimals1: int = 18,
    invert_price: bool = False
) -> Dict[str, Any]:
    """Decodes PancakeSwap V2 / Uniswap V2 getReserves() response."""
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
        "spot_price_usd": round(spot_price, 6)
    }


class UnifiedCommissionEngine:
    """
    Evaluates DEX Flash Swap Arbitrage and Keeper Call-Fee opportunities inside the same
    single-process engine as Flash Loan Liquidations, ranking all results in a unified queue.
    """

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.min_net_profit_usd = float(config.get("min_net_profit_usd", 1.50))

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
        Simulates an atomic Flash Swap arbitrage between Pool A and Pool B in a single transaction.
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

        # Deduct both pool swap fees + price impact slippage (8 bps) + on-chain gas
        total_fee_rate = pool_a_fee_rate + pool_b_fee_rate + 0.0008
        dex_fees_and_slippage_usd = trade_notional_usd * total_fee_rate
        gas_cost_usd = ((gas_limit * gas_price_gwei) / 1e9) * native_token_price_usd

        net_profit_usd = round(gross_spread_usd - dex_fees_and_slippage_usd - gas_cost_usd, 4)
        would_execute = net_profit_usd >= self.min_net_profit_usd

        return {
            "strategy_type": "DEX_FLASH_SWAP_ARBITRAGE",
            "status": "PROFITABLE_OPPORTUNITY_LOGGED" if would_execute else "REVERTED_SPREAD_BELOW_COSTS",
            "would_execute_atomic": would_execute,
            "chain": chain,
            "pair": pair_symbol,
            "buy_pool": buy_pool,
            "sell_pool": sell_pool,
            "buy_price_usd": round(low_price, 6),
            "sell_price_usd": round(high_price, 6),
            "spread_bps": round(spread_ratio * 10000, 2),
            "trade_notional_usd": round(trade_notional_usd, 2),
            "gross_spread_usd": round(gross_spread_usd, 4),
            "dex_fees_and_slippage_usd": round(dex_fees_and_slippage_usd, 4),
            "gas_cost_usd": round(gas_cost_usd, 4),
            "net_profit_usd": net_profit_usd
        }

    def evaluate_keeper_harvest_or_kick(
        self,
        vault_id: str,
        chain: str,
        pending_rewards_usd: float,
        call_fee_rate: float,
        flat_incentive_usd: float,
        native_token_price_usd: Optional[float],
        gas_price_gwei: Optional[float],
        oracle_is_valid: bool = True,
        gas_limit: int = 220000
    ) -> Dict[str, Any]:
        """
        Evaluates whether calling a public vault harvest() or auction kick() function yields
        net profit above gas and minimum threshold.
        """
        if (
            not oracle_is_valid
            or native_token_price_usd is None or native_token_price_usd <= 0.0
            or gas_price_gwei is None or gas_price_gwei <= 0.0
            or pending_rewards_usd < 0.0
        ):
            return {
                "strategy_type": "KEEPER_HARVEST_AND_KICK",
                "status": "ABORTED_INVALID_TELEMETRY",
                "would_execute_atomic": False,
                "chain": chain,
                "vault_id": vault_id,
                "net_profit_usd": 0.0
            }

        gross_commission_usd = (pending_rewards_usd * call_fee_rate) + flat_incentive_usd
        gas_cost_usd = ((gas_limit * gas_price_gwei) / 1e9) * native_token_price_usd
        net_profit_usd = round(gross_commission_usd - gas_cost_usd, 4)
        would_execute = net_profit_usd >= self.min_net_profit_usd

        return {
            "strategy_type": "KEEPER_HARVEST_AND_KICK",
            "status": "PROFITABLE_OPPORTUNITY_LOGGED" if would_execute else "SKIPPED_COMMISSION_BELOW_THRESHOLD",
            "would_execute_atomic": would_execute,
            "chain": chain,
            "vault_id": vault_id,
            "pending_rewards_usd": round(pending_rewards_usd, 4),
            "call_fee_bps": round(call_fee_rate * 10000, 2),
            "flat_incentive_usd": round(flat_incentive_usd, 4),
            "gross_commission_usd": round(gross_commission_usd, 4),
            "gas_cost_usd": round(gas_cost_usd, 4),
            "net_profit_usd": net_profit_usd
        }

    @staticmethod
    def rank_unified_opportunities(opportunities: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Central Profit Priority Queue:
        Filters only executable profitable opportunities across all strategies
        (Liquidations, DEX Arbitrage, Keeper Harvest/Kick) and orders them from highest
        to lowest net_profit_usd so the single wallet nonce dispatches the highest earner first.
        """
        executable = [
            opp for opp in opportunities
            if opp.get("would_execute_atomic") is True and float(opp.get("net_profit_usd", 0.0)) > 0.0
        ]
        return sorted(executable, key=lambda x: float(x.get("net_profit_usd", 0.0)), reverse=True)
