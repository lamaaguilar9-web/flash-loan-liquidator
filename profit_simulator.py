"""
Flash Loan Liquidator (Module C) - Phase 1 Shadow Mode
Deterministic Net Profit Simulator (Fail-Closed Atomic Evaluator).

Calculates exact simulated PnL for an atomic Flash Loan liquidation:
  Net Profit = Seized Collateral - Debt Covered - Flash Loan Fee - DEX Swap & Slippage - Gas Cost
Enforces strict fail-closed rejection if oracle data is stale/invalid, DEX liquidity is zero,
position is healthy (HF >= 1.0), or net profit falls below the minimum USD threshold.
"""

from typing import Dict, Any, Optional


class ProfitSimulator:
    """Evaluates net profitability of Flash Loan liquidations under strict fail-closed rules."""

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.min_net_profit_usd = float(config.get("min_net_profit_usd", 1.50))
        self.max_flash_loan_usd = float(config.get("max_flash_loan_usd", 250000.0))
        self.default_gas_limit = int(config.get("default_gas_limit", 450000))

    def evaluate_liquidation(
        self,
        position: Dict[str, Any],
        market: Dict[str, Any],
        native_token_price_usd: Optional[float],
        gas_price_gwei: Optional[float],
        oracle_is_valid: bool = True,
        pool_in_range_liquidity: Optional[int] = None,
        gas_limit: Optional[int] = None
    ) -> Dict[str, Any]:
        """
        Simulates an atomic Flash Loan liquidation and returns the complete breakdown.
        """
        borrower = position.get("borrower", "0x0")
        market_id = market.get("market_id", "unknown")
        chain = market.get("chain", "BNB")
        hf = float(position.get("health_factor", 999.0))
        total_debt_usd = float(position.get("total_debt_usd", 0.0))
        total_collateral_usd = float(position.get("total_collateral_usd", 0.0))

        # Gate 1: Oracle & Gas telemetry validity (Fail-Closed)
        if (
            not oracle_is_valid
            or native_token_price_usd is None
            or native_token_price_usd <= 0.0
            or gas_price_gwei is None
            or gas_price_gwei <= 0.0
        ):
            return {
                "status": "ABORTED_ORACLE_OR_GAS_INVALID",
                "would_execute_atomic": False,
                "chain": chain,
                "market_id": market_id,
                "borrower": borrower,
                "health_factor": hf,
                "net_profit_usd": 0.0,
                "reason": "Fail-closed block: missing or stale oracle price / gas telemetry"
            }

        # Gate 2: DEX Pool Liquidity Sanity Check
        if pool_in_range_liquidity is not None and pool_in_range_liquidity <= 0:
            return {
                "status": "ABORTED_ZERO_DEX_LIQUIDITY",
                "would_execute_atomic": False,
                "chain": chain,
                "market_id": market_id,
                "borrower": borrower,
                "health_factor": hf,
                "net_profit_usd": 0.0,
                "reason": "Fail-closed block: target DEX pool has zero in-range liquidity"
            }

        # Gate 3: Position Health Factor Check (HF < 1.0 required)
        if (
            not position.get("is_valid", False)
            or not position.get("is_liquidatable", False)
            or hf >= 1.0
            or total_debt_usd <= 0.0
        ):
            return {
                "status": "SKIPPED_HEALTHY_POSITION",
                "would_execute_atomic": False,
                "chain": chain,
                "market_id": market_id,
                "borrower": borrower,
                "health_factor": hf,
                "total_collateral_usd": total_collateral_usd,
                "total_debt_usd": total_debt_usd,
                "net_profit_usd": 0.0,
                "reason": "Position is healthy (Health Factor >= 1.0) or has zero debt"
            }

        # Step 1: Debt to cover via Flash Loan
        close_factor = float(market.get("close_factor", 0.50))
        liquidation_bonus = float(market.get("liquidation_bonus", 0.08))
        flash_fee_rate = float(market.get("flash_loan_fee_rate", 0.0005))
        dex_swap_fee_rate = float(market.get("dex_swap_fee_rate", 0.0005))

        max_repayable_usd = total_debt_usd * close_factor
        debt_to_cover_usd = min(max_repayable_usd, self.max_flash_loan_usd)

        # Step 2: Gross Collateral Seized (capped at borrower's actual total collateral)
        uncapped_seized_usd = debt_to_cover_usd * (1.0 + liquidation_bonus)
        collateral_seized_usd = min(uncapped_seized_usd, total_collateral_usd)
        gross_bonus_usd = collateral_seized_usd - debt_to_cover_usd

        # Step 3: Flash Loan Fee
        flash_loan_fee_usd = debt_to_cover_usd * flash_fee_rate

        # Step 4: DEX Swap Fee + Dynamic Price Impact Slippage
        base_slippage_rate = 0.0010  # 10 bps baseline execution slippage
        size_impact_rate = min(0.05, (collateral_seized_usd / 5_000_000.0) * 0.005)
        if pool_in_range_liquidity is not None and pool_in_range_liquidity < (10 ** 18):
            # Penalize slippage if active tick liquidity is thin
            size_impact_rate = min(0.08, size_impact_rate + 0.015)

        effective_slippage_rate = base_slippage_rate + size_impact_rate
        dex_swap_cost_usd = collateral_seized_usd * (dex_swap_fee_rate + effective_slippage_rate)

        # Step 5: On-Chain Gas Cost in USD
        used_gas_limit = int(gas_limit if gas_limit is not None else self.default_gas_limit)
        gas_cost_native = (used_gas_limit * gas_price_gwei) / 1e9
        gas_cost_usd = gas_cost_native * native_token_price_usd

        # Step 6: Net Profit & Fail-Closed Threshold Gate
        net_profit_usd = round(
            gross_bonus_usd - flash_loan_fee_usd - dex_swap_cost_usd - gas_cost_usd,
            4
        )
        would_execute = net_profit_usd >= self.min_net_profit_usd

        return {
            "status": "PROFITABLE_OPPORTUNITY_LOGGED" if would_execute else "REVERTED_NET_PROFIT_BELOW_THRESHOLD",
            "would_execute_atomic": would_execute,
            "chain": chain,
            "market_id": market_id,
            "protocol": market.get("protocol", market_id),
            "flash_loan_provider": market.get("flash_loan_provider", "PancakeSwap_V3"),
            "borrower": borrower,
            "health_factor": hf,
            "total_collateral_usd": round(total_collateral_usd, 4),
            "total_debt_usd": round(total_debt_usd, 4),
            "debt_to_cover_usd": round(debt_to_cover_usd, 4),
            "collateral_seized_usd": round(collateral_seized_usd, 4),
            "liquidation_bonus_pct": round(liquidation_bonus * 100.0, 2),
            "gross_bonus_usd": round(gross_bonus_usd, 4),
            "flash_loan_fee_usd": round(flash_loan_fee_usd, 4),
            "dex_swap_cost_usd": round(dex_swap_cost_usd, 4),
            "effective_slippage_bps": round(effective_slippage_rate * 10000, 2),
            "gas_limit": used_gas_limit,
            "gas_price_gwei": round(gas_price_gwei, 4),
            "native_token_price_usd": round(native_token_price_usd, 4),
            "gas_cost_usd": round(gas_cost_usd, 4),
            "net_profit_usd": net_profit_usd,
            "min_net_profit_threshold_usd": self.min_net_profit_usd,
            "reason": (
                "Net profit exceeds minimum threshold after flash loan fee, slippage, and gas"
                if would_execute
                else "Atomic transaction would revert: net profit below minimum threshold"
            )
        }
