"""
Flash Loan Liquidator (Module C) - Unified 5-in-1 On-Chain Commission Engine CLI.

100% Read-Only On-Chain Scanner executing:
  - 2 evaluadores económicos en vivo (liquidaciones Venus/Kinza/Aave + arbitraje DEX)
  - 3 monitores on-chain en vivo (Lista/Beefy/GMX)

Detalle de módulos:
  1. Bot #1 [Evaluador Económico en Vivo]: Liquidaciones con Flash Loans (Venus HF estimado / Kinza & Aave V3 HF nativo)
  2. Bot #2 [Evaluador Económico en Vivo]: Arbitraje Atómico DEX-a-DEX con Flash Swaps (PancakeSwap V3 <-> V2 en vivo)
  3. Bot #3 [Monitor On-Chain en Vivo]: Subastas Lista DAO lisUSD (0x0782...41E5 totalSupply() en vivo; kick PnL en banco de prueba)
  4. Bot #4 [Monitor On-Chain en Vivo]: Bóvedas Beefy Finance BSC (balance() LP en vivo; harvest PnL en banco de prueba)
  5. Bot #5 [Monitor On-Chain en Vivo]: Perpetuos GMX Vault Arbitrum (0x489e...97C4A getMaxPrice() en vivo; keeper PnL en banco de prueba)
"""

import os
import json
import time
import argparse
import logging
from typing import Dict, Any, List, Optional

from oracle_watcher import MultiChainRPCClient, OracleWatcher
from position_indexer import PositionIndexer, encode_address_param
from profit_simulator import ProfitSimulator
from commission_strategies import (
    SELECTOR_GET_RESERVES_V2,
    SELECTOR_VAULT_BALANCE,
    decode_uniswap_v2_reserves,
    UnifiedCommissionEngine
)
from shadow_logger import ShadowLogger
from telegram_notifier import TelegramCommissionNotifier
from dashboard_generator import write_dashboard_files

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("ShadowScanner")

DEFAULT_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config", "markets.json")

SELECTOR_TOTAL_SUPPLY = "0x18160ddd"     # totalSupply() -> uint256
SELECTOR_GMX_GET_MAX_PRICE = "0xe124e6d2" # getMaxPrice(address) -> uint256 (30 decimals)


class FlashLoanShadowScanner:
    """Orchestrates the 5-in-1 Shadow Mode Commission Engine across BNB Chain and Arbitrum."""

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
        self.unified_engine = UnifiedCommissionEngine(self.config)
        self.shadow_logger = shadow_logger or ShadowLogger()
        self.telegram_notifier = TelegramCommissionNotifier()

    def run_scan_cycle(self, chains: Optional[List[str]] = None, simulate_stress: bool = False) -> Dict[str, Any]:
        """Executes one complete read-only scan cycle for all 5 commission strategies in one memory loop."""
        t0 = time.perf_counter()
        target_chains = chains or ["BNB", "ARBITRUM"]

        chain_states: Dict[str, Dict[str, Any]] = {}
        oracle_snapshots: Dict[str, Dict[str, Any]] = {}
        for chain in target_chains:
            chain_states[chain] = self.rpc.get_chain_state(chain)
            oracle_snapshots[chain] = self.oracle_watcher.fetch_all_oracles(chain)

        # Live DEX Pool reads on BNB Chain (PancakeSwap V3 & V2) for Bot #1 and Bot #2.
        # Uniswap V2/V3 sort tokens lexicographically by address (token0 < token1):
        # USDT (0x55d3...) < WBNB (0xbb4C...), so token0=USDT and token1=WBNB.
        v3_cfg = self.config.get("dex_liquidity_pools", {}).get("PancakeSwap_V3_WBNB_USDT", {})
        dex_v3_state = self.oracle_watcher.fetch_dex_pool_state("PancakeSwap_V3_WBNB_USDT")
        pool_liq = dex_v3_state.get("in_range_liquidity") if dex_v3_state.get("is_valid") else None
        v3_invert = bool(v3_cfg.get("invert_price", True))
        if dex_v3_state.get("is_valid") and dex_v3_state.get("spot_price", 0) > 0:
            v3_wbnb_price = (1.0 / dex_v3_state["spot_price"]) if v3_invert else float(dex_v3_state["spot_price"])
        else:
            v3_wbnb_price = None

        v2_cfg = self.config.get("dex_liquidity_pools", {}).get("PancakeSwap_V2_WBNB_USDT", {})
        v2_addr = v2_cfg.get("address", "0x16b9a82891338f9bA80E2D6970FddA79D1eb0daE")
        v2_invert = bool(v2_cfg.get("invert_price", False))
        raw_v2_res = self.rpc.eth_call("BNB", v2_addr, SELECTOR_GET_RESERVES_V2) if "BNB" in target_chains else None
        dex_v2_state = decode_uniswap_v2_reserves(
            raw_v2_res or "",
            decimals0=int(v2_cfg.get("token0_decimals", 18)),
            decimals1=int(v2_cfg.get("token1_decimals", 18)),
            invert_price=v2_invert
        )
        v2_wbnb_price = dex_v2_state.get("spot_price_usd") if dex_v2_state.get("is_valid") else None

        # Live Bot #2 Check: Real-time spread between PancakeSwap V3 and V2
        live_dex_arb_check: Optional[Dict[str, Any]] = None
        if "BNB" in target_chains and v3_wbnb_price and v2_wbnb_price:
            bnb_state = chain_states.get("BNB", {})
            bnb_feed = oracle_snapshots.get("BNB", {}).get("BNB_USD", {})
            live_dex_arb_check = self.unified_engine.evaluate_dex_flash_arbitrage(
                pair_symbol="WBNB/USDT",
                chain="BNB",
                pool_a_name="PancakeSwap_V3_5bps",
                pool_a_price_usd=v3_wbnb_price,
                pool_a_fee_rate=0.0005,
                pool_b_name="PancakeSwap_V2_25bps",
                pool_b_price_usd=v2_wbnb_price,
                pool_b_fee_rate=0.0025,
                trade_notional_usd=10000.0,
                native_token_price_usd=bnb_feed.get("price_usd"),
                gas_price_gwei=bnb_state.get("gas_price_gwei"),
                oracle_is_valid=bool(bnb_feed.get("is_valid", False))
            )

        # Live Bot #3 Check: On-chain Lista DAO lisUSD debt supply & auction readiness on BNB Chain
        live_bot3_lista: Dict[str, Any] = {}
        if "BNB" in target_chains:
            for mkt in self.config.get("auction_kick_markets", []):
                raw_sup = self.rpc.eth_call("BNB", mkt["token_address"], SELECTOR_TOTAL_SUPPLY)
                lis_supply = round(int(raw_sup, 16) / 1e18, 2) if (raw_sup and raw_sup.startswith("0x") and len(raw_sup) > 2) else None
                live_bot3_lista = {
                    "protocol": mkt["protocol"],
                    "token_address": mkt["token_address"],
                    "live_lisusd_supply": lis_supply,
                    "min_collateral_ratio": mkt["min_collateral_ratio"],
                    "status": "ONLINE_MONITORING" if lis_supply else "RPC_UNAVAILABLE"
                }

        # Live Bot #4 Check: On-chain Beefy Finance BSC Vaults balance() queries
        live_bot4_beefy: List[Dict[str, Any]] = []
        if "BNB" in target_chains:
            for vmeta in self.config.get("beefy_harvest_vaults", []):
                raw_bal = self.rpc.eth_call("BNB", vmeta["vault_address"], SELECTOR_VAULT_BALANCE)
                lp_bal = round(int(raw_bal, 16) / 1e18, 6) if (raw_bal and raw_bal.startswith("0x") and len(raw_bal) > 2) else None
                live_bot4_beefy.append({
                    "vault_id": vmeta["vault_id"],
                    "vault_address": vmeta["vault_address"],
                    "live_lp_balance": lp_bal,
                    "call_fee_bps": round(float(vmeta.get("call_fee_rate", 0.005)) * 10000, 1),
                    "status": "ONLINE_MONITORING" if lp_bal is not None else "RPC_UNAVAILABLE"
                })

        # Live Bot #5 Check: On-chain GMX Vault Arbitrum getMaxPrice(WETH) query
        live_bot5_gmx: Dict[str, Any] = {}
        if "ARBITRUM" in target_chains:
            for pmeta in self.config.get("perp_keeper_vaults", []):
                padded_weth = encode_address_param(pmeta["index_token_weth"])
                raw_gmx_p = self.rpc.eth_call("ARBITRUM", pmeta["vault_address"], SELECTOR_GMX_GET_MAX_PRICE + padded_weth)
                gmx_weth_usd = round(int(raw_gmx_p, 16) / 1e30, 4) if (raw_gmx_p and raw_gmx_p.startswith("0x") and len(raw_gmx_p) > 2) else None
                live_bot5_gmx = {
                    "protocol": pmeta["protocol"],
                    "vault_address": pmeta["vault_address"],
                    "live_weth_max_price_usd": gmx_weth_usd,
                    "status": "ONLINE_MONITORING" if gmx_weth_usd else "RPC_UNAVAILABLE"
                }

        # Live Bot #1 Check: Lending Markets Scan
        markets = [
            m for m in self.config.get("lending_markets", [])
            if m.get("chain") in target_chains
        ]

        scanned_positions: List[Dict[str, Any]] = []
        watchlist_positions: List[Dict[str, Any]] = []
        live_opportunities: List[Dict[str, Any]] = []

        if live_dex_arb_check and live_dex_arb_check.get("would_execute_atomic"):
            live_opportunities.append(live_dex_arb_check)

        for market in markets:
            chain = market["chain"]
            c_state = chain_states.get(chain, {})
            latest_block = c_state.get("block_number")
            gas_price_gwei = c_state.get("gas_price_gwei")

            chain_oracles = oracle_snapshots.get(chain, {})
            oracle_price_map = {
                k: float(v["price_usd"])
                for k, v in chain_oracles.items()
                if v.get("is_valid") and v.get("price_usd") is not None
            }
            native_feed_key = "BNB_USD" if chain == "BNB" else "ETH_USD"
            native_feed = chain_oracles.get(native_feed_key, {})
            oracle_valid = bool(native_feed.get("is_valid", False))
            native_price_usd = native_feed.get("price_usd")

            borrowers = self.position_indexer.discover_recent_borrowers(market, latest_block, max_candidates=6)
            for borrower in borrowers:
                pos = self.position_indexer.evaluate_borrower(market, borrower, oracle_prices=oracle_price_map)
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
                    eval_res["strategy_id"] = "BOT_1_FLASH_LIQUIDATION"
                    eval_res["strategy_type"] = "LIQUIDATION_FLASH_LOAN"
                    eval_res["hf_source"] = pos.get("hf_source")
                    eval_res["block_number"] = latest_block
                    self.shadow_logger.log_liquidation_evaluation(eval_res, is_test=False)
                    live_opportunities.append(eval_res)

        stress_evaluations: List[Dict[str, Any]] = []
        stress_priority_queue: List[Dict[str, Any]] = []
        if simulate_stress and "BNB" in chain_states:
            bnb_state = chain_states["BNB"]
            bnb_feed = oracle_snapshots.get("BNB", {}).get("BNB_USD", {})
            bnb_price = bnb_feed.get("price_usd")
            bnb_gas = bnb_state.get("gas_price_gwei")
            bnb_valid = bool(bnb_feed.get("is_valid", False))
            venus_market = next((m for m in markets if m["market_id"] == "Venus_Core_BSC"), markets[0])

            arb_state = chain_states.get("ARBITRUM", bnb_state)
            arb_eth_feed = oracle_snapshots.get("ARBITRUM", {}).get("ETH_USD") or oracle_snapshots.get("BNB", {}).get("ETH_USD", {})
            arb_eth_price = arb_eth_feed.get("price_usd") or bnb_price
            arb_gas = arb_state.get("gas_price_gwei") or bnb_gas
            arb_valid = bool(arb_eth_feed.get("is_valid", bnb_valid))

            ev1 = self.profit_simulator.evaluate_liquidation(
                position={
                    "is_valid": True,
                    "is_liquidatable": True,
                    "architecture": "compound_v2",
                    "market_id": venus_market["market_id"],
                    "borrower": "0x000000000000000000000000000000000000dEaD",
                    "total_collateral_usd": 14500.0,
                    "total_debt_usd": 12000.0,
                    "shortfall_usd": 720.0,
                    "health_factor": 0.9434,
                    "hf_source": "HF estimado (derivado de getAccountLiquidity)"
                },
                market=venus_market,
                native_token_price_usd=bnb_price,
                gas_price_gwei=bnb_gas,
                oracle_is_valid=bnb_valid,
                pool_in_range_liquidity=pool_liq
            )
            ev1["strategy_id"] = "BOT_1_FLASH_LIQUIDATION"
            ev1["strategy_type"] = "LIQUIDATION_FLASH_LOAN"
            ev1["hf_source"] = "HF estimado (derivado de getAccountLiquidity)"

            ev2 = self.unified_engine.evaluate_dex_flash_arbitrage(
                pair_symbol="WBNB/USDT",
                chain="BNB",
                pool_a_name="PancakeSwap_V3",
                pool_a_price_usd=bnb_price or 727.0,
                pool_a_fee_rate=0.0005,
                pool_b_name="Thena_Algebra_BSC",
                pool_b_price_usd=(bnb_price or 727.0) * 1.0065,
                pool_b_fee_rate=0.0015,
                trade_notional_usd=8000.0,
                native_token_price_usd=bnb_price,
                gas_price_gwei=bnb_gas,
                oracle_is_valid=bnb_valid
            )

            ev3 = self.unified_engine.evaluate_auction_kick_bounty(
                protocol="Lista_DAO_lisUSD",
                chain="BNB",
                vault_owner="0x000000000000000000000000000000000000bEEF",
                collateral_usd=5200.0,
                debt_usd=4900.0,
                min_collateral_ratio=1.10,
                flat_kick_tip_usd=15.0,
                percentage_kick_bonus_rate=0.01,
                native_token_price_usd=bnb_price,
                gas_price_gwei=bnb_gas,
                oracle_is_valid=bnb_valid
            )

            ev4 = self.unified_engine.evaluate_vault_harvest_keeper(
                vault_id="pancake-cow-bsc-eth-wbnb-vault",
                chain="BNB",
                pending_rewards_usd=1850.0,
                call_fee_rate=0.005,
                native_token_price_usd=bnb_price,
                gas_price_gwei=bnb_gas,
                oracle_is_valid=bnb_valid
            )

            # N1: GMX_Vault_Arbitrum runs on ARBITRUM chain using live Arbitrum ETH price & gas
            ev5 = self.unified_engine.evaluate_perp_order_keeper(
                protocol="GMX_Vault_Arbitrum",
                chain="ARBITRUM",
                order_id="ORD-ARB-90412",
                order_type="LIQUIDATION",
                direction="LONG",
                trigger_price_usd=(arb_eth_price or 2600.0) + 5.0,
                current_oracle_price_usd=arb_eth_price,
                position_size_usd=15000.0,
                execution_fee_usd=2.50,
                keeper_bonus_rate=0.002,
                native_token_price_usd=arb_eth_price,
                gas_price_gwei=arb_gas,
                oracle_is_valid=arb_valid
            )

            stress_evaluations = [ev1, ev2, ev3, ev4, ev5]
            for ev in stress_evaluations:
                ev_chain = ev.get("chain", "BNB")
                ev["block_number"] = chain_states.get(ev_chain, bnb_state).get("block_number")
                ev["stress_test_case"] = True
                self.shadow_logger.log_liquidation_evaluation(ev, is_test=True)

            stress_priority_queue = self.unified_engine.rank_unified_opportunities(stress_evaluations)

        elapsed_ms = round((time.perf_counter() - t0) * 1000, 2)
        summary = {
            "elapsed_ms": elapsed_ms,
            "architecture_scope": (
                "2 evaluadores económicos en vivo (liquidaciones Venus/Kinza/Aave + arbitraje DEX) "
                "+ 3 monitores on-chain en vivo (Lista/Beefy/GMX)"
            ),
            "chains": chain_states,
            "oracles": oracle_snapshots,
            "dex_v3_pool": dex_v3_state,
            "dex_v2_pool": dex_v2_state,
            "live_dex_arb_check": live_dex_arb_check,
            "live_bot3_lista": live_bot3_lista,
            "live_bot4_beefy": live_bot4_beefy,
            "live_bot5_gmx": live_bot5_gmx,
            "positions_scanned_count": len(scanned_positions),
            "watchlist_count": len(watchlist_positions),
            "live_opportunities_count": len(live_opportunities),
            "live_priority_queue": self.unified_engine.rank_unified_opportunities(live_opportunities),
            "stress_evaluations": stress_evaluations,
            "stress_priority_queue": stress_priority_queue
        }
        for live_opp in summary["live_priority_queue"]:
            self.telegram_notifier.notify_opportunity(live_opp, is_live_execution=False)

        write_dashboard_files(summary, telegram_configured=self.telegram_notifier.is_configured)
        self.shadow_logger.log_telemetry_cycle(summary, is_test=simulate_stress)
        return summary


def print_console_report(summary: Dict[str, Any]) -> None:
    """Formats a console report of the Unified 5-in-1 Shadow Mode Commission Engine."""
    print("=" * 84)
    print(" SENTINEL FLEET — MOTOR ÚNICO 5-EN-1 DE COMISIONES ON-CHAIN (MODO SOMBRA)")
    print(" Alcance: 2 evaluadores económicos en vivo (liquidaciones Venus/Kinza/Aave + arbitraje DEX)")
    print("        + 3 monitores on-chain en vivo (Lista/Beefy/GMX)")
    print("=" * 84)
    for chain, st in summary.get("chains", {}).items():
        print(
            f"[*] Chain: {chain:<8} | Block: {str(st.get('block_number')):<10} | "
            f"Gas: {str(st.get('gas_price_gwei')):<7} Gwei | RPC Latency: {st.get('rpc_latency_ms')} ms"
        )

    print("-" * 84)
    print("[*] Oráculos Chainlink en Vivo (Verificación Fail-Closed):")
    for chain, feeds in summary.get("oracles", {}).items():
        for sym, fdata in feeds.items():
            status_str = "VALID" if fdata.get("is_valid") else f"INVALID ({fdata.get('reason')})"
            print(
                f"    - [{chain}] {sym:<8}: ${str(fdata.get('price_usd')):<12} | "
                f"Round: {str(fdata.get('round_id')):<22} | Age: {fdata.get('age_seconds')}s | [{status_str}]"
            )

    print("-" * 84)
    print(
        f"[*] Bot #1 [Evaluador Económico en Vivo] Préstamos Escaneados (Venus HF estimado derivado de getAccountLiquidity / Kinza & Aave HF nativo): "
        f"{summary.get('positions_scanned_count')} | En Vigilancia: {summary.get('watchlist_count')}"
    )

    arb_live = summary.get("live_dex_arb_check")
    if arb_live:
        print(
            f"[*] Bot #2 [Evaluador Económico en Vivo] Arbitraje DEX (WBNB/USDT): Buy {arb_live.get('buy_pool')}=${arb_live.get('buy_price_usd')} | "
            f"Sell {arb_live.get('sell_pool')}=${arb_live.get('sell_price_usd')} | Spread={arb_live.get('spread_bps')} bps [{arb_live.get('status')}]"
        )

    b3 = summary.get("live_bot3_lista", {})
    if b3:
        print(f"[*] Bot #3 [Monitor On-Chain en Vivo] Subastas Lista DAO ({b3.get('token_address')}): Supply={b3.get('live_lisusd_supply')} lisUSD [{b3.get('status')}]")

    b4_list = summary.get("live_bot4_beefy", [])
    for b4 in b4_list:
        print(f"[*] Bot #4 [Monitor On-Chain en Vivo] Bóveda Beefy ({b4.get('vault_id')}): Balance LP={b4.get('live_lp_balance')} | CallFee={b4.get('call_fee_bps')} bps [{b4.get('status')}]")

    b5 = summary.get("live_bot5_gmx", {})
    if b5:
        print(f"[*] Bot #5 [Monitor On-Chain en Vivo] Perpetuos GMX Arbitrum ({b5.get('vault_address')}): WETH MaxPrice=${b5.get('live_weth_max_price_usd')} [{b5.get('status')}]")

    live_q = summary.get("live_priority_queue", [])
    print("-" * 84)
    print(f"[*] Cola Central de Prioridad EN VIVO (Oportunidades Reales Detectadas): {len(live_q)}")
    for idx, ev in enumerate(live_q, 1):
        print(
            f"    #{idx} [{ev.get('strategy_id'):<28}] | Gas=${ev.get('gas_cost_usd'):<7} | "
            f"PnL Neto=${ev.get('net_profit_usd'):<9} -> [OPORTUNIDAD REAL #{idx}]"
        )

    stress_q = summary.get("stress_priority_queue", [])
    if stress_q:
        print("-" * 84)
        print("[*] Banco de Prueba Matemática [SIMULACIÓN DE ESTRÉS — DATOS SINTÉTICOS, NO REAL]:")
        for idx, ev in enumerate(stress_q, 1):
            print(
                f"    #{idx} [{ev.get('strategy_id'):<28}] | Gas=${ev.get('gas_cost_usd'):<7} | "
                f"PnL Ficticio=${ev.get('net_profit_usd'):<9} -> [SIMULACIÓN DE ESTRÉS #{idx}]"
            )
    print("=" * 84, flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Sentinel Fleet 5-in-1 On-Chain Commission Engine - Shadow Mode")
    parser.add_argument("--chains", nargs="+", default=["BNB", "ARBITRUM"], help="Chains to scan (BNB ARBITRUM)")
    parser.add_argument("--cycles", type=int, default=1, help="Number of scan cycles to run (0 for continuous daemon)")
    parser.add_argument("--interval", type=int, default=15, help="Seconds between scan cycles")
    parser.add_argument("--simulate-stress", action="store_true", help="Evaluate all 5 bots using live network gas & Chainlink prices")
    args = parser.parse_args()

    scanner = FlashLoanShadowScanner()
    cycle_idx = 0
    while args.cycles == 0 or cycle_idx < args.cycles:
        cycle_idx += 1
        try:
            summary = scanner.run_scan_cycle(chains=args.chains, simulate_stress=args.simulate_stress)
            print_console_report(summary)
        except Exception as exc:
            logger.error(f"Cycle #{cycle_idx} error: {exc}")
        if args.cycles == 0 or cycle_idx < args.cycles:
            time.sleep(args.interval)


if __name__ == "__main__":
    main()
