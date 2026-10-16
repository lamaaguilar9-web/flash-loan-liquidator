"""
Flash Loan Liquidator (Module C) - Phase 1 Shadow Mode
Main Shadow Scanner CLI & Orchestrator.

100% Read-Only On-Chain Scanner:
  - Reads live Chainlink AggregatorV3 price feeds on BNB Chain & Arbitrum
  - Reads live PancakeSwap V3 pool liquidity & slot0
  - Scans active borrower positions on Venus Protocol, Kinza Finance, and Aave V3
  - Evaluates net Flash Loan liquidation profitability with fail-closed gates
  - Persists SHA-256 attested records in data/shadow_telemetry.jsonl and data/shadow_liquidations.jsonl
"""

import os
import json
import time
import argparse
import logging
from typing import Dict, Any, List, Optional

from oracle_watcher import MultiChainRPCClient, OracleWatcher
from position_indexer import PositionIndexer
from profit_simulator import ProfitSimulator
from shadow_logger import ShadowLogger

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("ShadowScanner")

DEFAULT_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config", "markets.json")


class FlashLoanShadowScanner:
    """Orchestrates Phase 1 Shadow Mode scanning across BNB Chain and Arbitrum."""

    def __init__(
        self,
        config_path: str = DEFAULT_CONFIG_PATH,
        rpc_client: Optional[MultiChainRPCClient] = None,
        shadow_logger: Optional[ShadowLogger] = None
    ):
        with open(config_path, "r", encoding="utf-8") as f:
            self.config: Dict[str, Any] = json.load(f)

        self.rpc = rpc_client or MultiChainRPCClient(self.config.get("rpc_endpoints", {}))
        self.oracle_watcher = OracleWatcher(self.rpc, self.config)
        self.position_indexer = PositionIndexer(self.rpc, self.config)
        self.profit_simulator = ProfitSimulator(self.config)
        self.shadow_logger = shadow_logger or ShadowLogger()

    def run_scan_cycle(self, chains: Optional[List[str]] = None, simulate_stress: bool = False) -> Dict[str, Any]:
        """Executes one complete read-only scan cycle across configured chains and lending markets."""
        t0 = time.perf_counter()
        target_chains = chains or ["BNB", "ARBITRUM"]

        chain_states: Dict[str, Dict[str, Any]] = {}
        oracle_snapshots: Dict[str, Dict[str, Any]] = {}
        for chain in target_chains:
            chain_states[chain] = self.rpc.get_chain_state(chain)
            oracle_snapshots[chain] = self.oracle_watcher.fetch_all_oracles(chain)

        dex_state = self.oracle_watcher.fetch_dex_pool_state("PancakeSwap_V3_WBNB_USDT")
        pool_liq = dex_state.get("in_range_liquidity") if dex_state.get("is_valid") else None

        markets = [
            m for m in self.config.get("lending_markets", [])
            if m.get("chain") in target_chains
        ]

        scanned_positions: List[Dict[str, Any]] = []
        watchlist_positions: List[Dict[str, Any]] = []
        liquidatable_evaluations: List[Dict[str, Any]] = []

        for market in markets:
            chain = market["chain"]
            c_state = chain_states.get(chain, {})
            latest_block = c_state.get("block_number")
            gas_price_gwei = c_state.get("gas_price_gwei")

            chain_oracles = oracle_snapshots.get(chain, {})
            native_feed_key = "BNB_USD" if chain == "BNB" else "ETH_USD"
            native_feed = chain_oracles.get(native_feed_key, {})
            oracle_valid = bool(native_feed.get("is_valid", False))
            native_price_usd = native_feed.get("price_usd")

            borrowers = self.position_indexer.discover_recent_borrowers(market, latest_block, max_candidates=6)
            for borrower in borrowers:
                pos = self.position_indexer.evaluate_borrower(market, borrower)
                pos["chain"] = chain
                pos["block_number"] = latest_block
                scanned_positions.append(pos)

                if not pos.get("is_valid"):
                    continue

                hf = float(pos.get("health_factor", 999.0))
                if 1.0 <= hf < float(self.config.get("watchlist_hf_threshold", 1.08)):
                    watchlist_positions.append(pos)

                if pos.get("is_liquidatable") or hf < 1.0:
                    eval_res = self.profit_simulator.evaluate_liquidation(
                        position=pos,
                        market=market,
                        native_token_price_usd=native_price_usd,
                        gas_price_gwei=gas_price_gwei,
                        oracle_is_valid=oracle_valid,
                        pool_in_range_liquidity=pool_liq if chain == "BNB" else None
                    )
                    eval_res["block_number"] = latest_block
                    self.shadow_logger.log_liquidation_evaluation(eval_res, is_test=False)
                    liquidatable_evaluations.append(eval_res)

        stress_evaluations: List[Dict[str, Any]] = []
        if simulate_stress and "BNB" in chain_states:
            # Uses REAL live BNB Chain block, gas price, Chainlink BNB/USD price, and PancakeSwap V3 liquidity
            # to evaluate two deterministic synthetic positions (1 profitable, 1 unprofitable due to small size)
            bnb_state = chain_states["BNB"]
            bnb_feed = oracle_snapshots.get("BNB", {}).get("BNB_USD", {})
            venus_market = next((m for m in markets if m["market_id"] == "Venus_Core_BSC"), markets[0])

            synthetic_cases = [
                {
                    "is_valid": True,
                    "is_liquidatable": True,
                    "architecture": "compound_v2",
                    "market_id": venus_market["market_id"],
                    "borrower": "0x000000000000000000000000000000000000dEaD",
                    "total_collateral_usd": 14500.0,
                    "total_debt_usd": 12000.0,
                    "shortfall_usd": 720.0,
                    "health_factor": 0.9434
                },
                {
                    "is_valid": True,
                    "is_liquidatable": True,
                    "architecture": "compound_v2",
                    "market_id": venus_market["market_id"],
                    "borrower": "0x0000000000000000000000000000000000000001",
                    "total_collateral_usd": 15.0,
                    "total_debt_usd": 12.0,
                    "shortfall_usd": 0.50,
                    "health_factor": 0.9600
                }
            ]
            for syn_pos in synthetic_cases:
                ev = self.profit_simulator.evaluate_liquidation(
                    position=syn_pos,
                    market=venus_market,
                    native_token_price_usd=bnb_feed.get("price_usd"),
                    gas_price_gwei=bnb_state.get("gas_price_gwei"),
                    oracle_is_valid=bool(bnb_feed.get("is_valid", False)),
                    pool_in_range_liquidity=pool_liq
                )
                ev["block_number"] = bnb_state.get("block_number")
                ev["stress_test_case"] = True
                self.shadow_logger.log_liquidation_evaluation(ev, is_test=True)
                stress_evaluations.append(ev)

        elapsed_ms = round((time.perf_counter() - t0) * 1000, 2)
        summary = {
            "elapsed_ms": elapsed_ms,
            "chains": chain_states,
            "oracles": oracle_snapshots,
            "dex_pool": dex_state,
            "positions_scanned_count": len(scanned_positions),
            "watchlist_count": len(watchlist_positions),
            "live_liquidatable_count": len(liquidatable_evaluations),
            "live_liquidatable_evaluations": liquidatable_evaluations,
            "stress_evaluations": stress_evaluations
        }
        self.shadow_logger.log_telemetry_cycle(summary, is_test=simulate_stress)
        return summary


def print_console_report(summary: Dict[str, Any]) -> None:
    """Formats a clear console report of the Shadow Mode scan cycle."""
    print("=" * 80)
    print(" SENTINEL FLEET — FLASH LOAN LIQUIDATOR (FASE 1: MODO SOMBRA SOLO LECTURA)")
    print("=" * 80)
    for chain, st in summary.get("chains", {}).items():
        print(
            f"[*] Chain: {chain:<8} | Block: {str(st.get('block_number')):<10} | "
            f"Gas: {str(st.get('gas_price_gwei')):<7} Gwei | RPC Latency: {st.get('rpc_latency_ms')} ms"
        )

    print("-" * 80)
    print("[*] Oráculos Chainlink en Vivo (Verificación Fail-Closed):")
    for chain, feeds in summary.get("oracles", {}).items():
        for sym, fdata in feeds.items():
            status_str = "VALID" if fdata.get("is_valid") else f"INVALID ({fdata.get('reason')})"
            print(
                f"    - [{chain}] {sym:<8}: ${str(fdata.get('price_usd')):<12} | "
                f"Round: {str(fdata.get('round_id')):<22} | Age: {fdata.get('age_seconds')}s | [{status_str}]"
            )

    dex = summary.get("dex_pool", {})
    if dex:
        print("-" * 80)
        print(
            f"[*] Pool DEX ({dex.get('pool_key')}): Spot=${dex.get('spot_price')} | "
            f"Tick={dex.get('tick')} | In-Range Liquidity={dex.get('in_range_liquidity')}"
        )

    print("-" * 80)
    print(
        f"[*] Posiciones Escaneadas On-Chain: {summary.get('positions_scanned_count')} | "
        f"En Vigilancia (1.0 <= HF < 1.08): {summary.get('watchlist_count')} | "
        f"Liquidables en Vivo (HF < 1.0): {summary.get('live_liquidatable_count')}"
    )

    stress = summary.get("stress_evaluations", [])
    if stress:
        print("-" * 80)
        print("[*] Simulación Determinista con Parámetros Reales de Red (is_test=True):")
        for ev in stress:
            verdict = "EJECUTAR FLASH LOAN" if ev.get("would_execute_atomic") else "REVERTIR (NO RENTABLE)"
            print(
                f"    -> Borrower {ev['borrower'][:10]}... | HF={ev['health_factor']} | "
                f"Deuda Cubierta=${ev['debt_to_cover_usd']} | Bono Bruto=${ev['gross_bonus_usd']} | "
                f"Fee Flash=${ev['flash_loan_fee_usd']} | Slippage+DEX=${ev['dex_swap_cost_usd']} | "
                f"Gas=${ev['gas_cost_usd']} | PnL Neto=${ev['net_profit_usd']} -> [{verdict}]"
            )
    print("=" * 80)


def main() -> None:
    parser = argparse.ArgumentParser(description="Sentinel Fleet Flash Loan Liquidator - Phase 1 Shadow Scanner")
    parser.add_argument("--chains", nargs="+", default=["BNB", "ARBITRUM"], help="Chains to scan (BNB ARBITRUM)")
    parser.add_argument("--cycles", type=int, default=1, help="Number of scan cycles to run")
    parser.add_argument("--interval", type=int, default=15, help="Seconds between scan cycles")
    parser.add_argument("--simulate-stress", action="store_true", help="Include stress-test evaluation using live gas/oracle parameters")
    args = parser.parse_args()

    scanner = FlashLoanShadowScanner()
    for i in range(args.cycles):
        summary = scanner.run_scan_cycle(chains=args.chains, simulate_stress=args.simulate_stress)
        print_console_report(summary)
        if i + 1 < args.cycles:
            time.sleep(args.interval)


if __name__ == "__main__":
    main()
