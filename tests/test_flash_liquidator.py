"""
Unit and Integration Test Suite for Unified 5-in-1 On-Chain Commission Engine (Phase 1 - Shadow Mode).
Verifies all 5 commission strategies (Flash Loan Liquidations, DEX Flash Swap Arbitrage,
Lista DAO Auction Kick Bounties, Beefy Vault Harvest Keepers, and Perp Order/Liq Keepers)
plus fail-closed Chainlink decoding, read-only RPC enforcement, and Central Priority Queue ranking.
"""

import os
import sys
import tempfile
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from oracle_watcher import (
    decode_chainlink_latest_round_data,
    decode_uniswap_v3_slot0,
    MultiChainRPCClient,
    WriteMethodForbiddenError
)
from position_indexer import (
    decode_aave_v3_user_account_data,
    decode_compound_account_liquidity,
    decode_vtoken_borrow_balance_stored
)
from profit_simulator import ProfitSimulator
from commission_strategies import (
    decode_uniswap_v2_reserves,
    UnifiedCommissionEngine
)
from shadow_logger import ShadowLogger, compute_record_sha256


def _pack_words(*words: int) -> str:
    hex_chunks = []
    for w in words:
        if w < 0:
            w = (1 << 256) + w
        hex_chunks.append(f"{w:064x}")
    return "0x" + "".join(hex_chunks)


def test_01_chainlink_valid_round_decoding():
    now_ts = 1760570000
    raw = _pack_words(18446744073709562001, 72880824556, now_ts - 45, now_ts - 40, 18446744073709562001)
    res = decode_chainlink_latest_round_data(raw, decimals=8, max_staleness_seconds=3600, now_ts=now_ts)
    assert res["is_valid"] is True
    assert res["price_usd"] == pytest.approx(728.80824556)


def test_02_chainlink_fail_closed_negative_or_zero_answer():
    now_ts = 1760570000
    raw_zero = _pack_words(100, 0, now_ts - 10, now_ts - 5, 100)
    assert decode_chainlink_latest_round_data(raw_zero, decimals=8, now_ts=now_ts)["is_valid"] is False

    raw_neg = _pack_words(100, -50000000, now_ts - 10, now_ts - 5, 100)
    assert decode_chainlink_latest_round_data(raw_neg, decimals=8, now_ts=now_ts)["is_valid"] is False


def test_03_chainlink_fail_closed_stale_timestamp_and_incomplete_round():
    now_ts = 1760570000
    raw_stale = _pack_words(105, 60000000000, now_ts - 7200, now_ts - 7200, 105)
    assert decode_chainlink_latest_round_data(raw_stale, decimals=8, max_staleness_seconds=3600, now_ts=now_ts)["is_valid"] is False

    raw_inc = _pack_words(105, 60000000000, now_ts - 30, now_ts - 20, 104)
    assert decode_chainlink_latest_round_data(raw_inc, decimals=8, max_staleness_seconds=3600, now_ts=now_ts)["is_valid"] is False


def test_04_uniswap_v3_slot0_and_liquidity_decoding():
    raw_slot0 = _pack_words(1 << 96, -120, 0, 1, 1, 0, 1)
    res = decode_uniswap_v3_slot0(raw_slot0, hex(5000000000000000000000))
    assert res["is_valid"] is True
    assert res["spot_price"] == pytest.approx(1.0)


def test_05_rpc_client_blocks_write_methods():
    client = MultiChainRPCClient({"BNB": ["https://bsc-rpc.publicnode.com"]})
    with pytest.raises(PermissionError, match="Shadow Mode violation"):
        client.call_rpc("BNB", "eth_sendRawTransaction", ["0xdeadbeef"])


def test_06_aave_v3_user_account_decoding_underwater_and_healthy():
    borrower = "0x955b7a7ddcbf443d0e6b899ba1aba71f7413272f"
    raw_u = _pack_words(10000 * (10 ** 8), 8500 * (10 ** 8), 0, 8000, 7500, int(0.94 * (10 ** 18)))
    pos_u = decode_aave_v3_user_account_data(raw_u, borrower, "Kinza_Finance_BSC")
    assert pos_u["is_liquidatable"] is True
    assert pos_u["health_factor"] == pytest.approx(0.94)

    raw_h = _pack_words(15000 * (10 ** 8), 8500 * (10 ** 8), 2000 * (10 ** 8), 8000, 7500, int(1.45 * (10 ** 18)))
    pos_h = decode_aave_v3_user_account_data(raw_h, borrower, "Kinza_Finance_BSC")
    assert pos_h["is_liquidatable"] is False


def test_07_compound_venus_account_liquidity_decoding_and_error_gate():
    borrower = "0x955b7a7ddcbf443d0e6b899ba1aba71f7413272f"
    raw_err = _pack_words(3, 0, 500 * (10 ** 18))
    assert decode_compound_account_liquidity(raw_err, borrower, "Venus_Core_BSC")["is_valid"] is False

    raw_sf = _pack_words(0, 0, 1200 * (10 ** 18))
    res_sf = decode_compound_account_liquidity(raw_sf, borrower, "Venus_Core_BSC", measured_debt_usd=10000.0)
    assert res_sf["is_liquidatable"] is True
    assert res_sf["health_factor"] < 1.0

    # ALTA-2 regression: real 96-byte (194-char) VBep20Delegator response from vUSDT on BSC block ~128601311
    real_vusdt_96b = (
        "0x0000000000000000000000000000000000000000000016e91bfb08b9c1c0f7fa"
        "0000000000000000000000000000000000000000000000000000000000000000"
        "0000000000000000000000000000000000000000000000000000000000000000"
    )
    dec_96 = decode_vtoken_borrow_balance_stored(real_vusdt_96b, underlying_decimals=18)
    assert dec_96["is_valid"] is True
    assert dec_96["borrow_amount"] == pytest.approx(108192.170207, rel=1e-6)
    assert dec_96["borrow_amount"] < 1e7  # Never 1e159

    # Real Comptroller response for 0x955b7a7ddcbf443d0e6b899ba1aba71f7413272f ($129,964.84 excess liquidity)
    real_comptroller_hex = (
        "0x0000000000000000000000000000000000000000000000000000000000000000"
        "000000000000000000000000000000000000000000001b85fbfd4b787603bffe"
        "0000000000000000000000000000000000000000000000000000000000000000"
    )
    debt_usd = round(dec_96["borrow_amount"] * 0.99885, 4)
    pos_real = decode_compound_account_liquidity(real_comptroller_hex, borrower, "Venus_Core_BSC", measured_debt_usd=debt_usd)
    assert pos_real["is_valid"] is True
    assert pos_real["total_debt_usd"] == pytest.approx(108067.7492, rel=1e-4)
    assert pos_real["liquidity_excess_usd"] == pytest.approx(129964.8415, rel=1e-4)
    assert pos_real["health_factor"] == pytest.approx(2.202633, rel=1e-4)

    # ALTA-2 regression: normal 66-char (32-byte) response (e.g. vBNB)
    raw_32b = "0x" + hex(25 * (10 ** 18))[2:].zfill(64)
    dec_32 = decode_vtoken_borrow_balance_stored(raw_32b, underlying_decimals=18)
    assert dec_32["is_valid"] is True
    assert dec_32["borrow_amount"] == pytest.approx(25.0)

    # ALTA-2 regression: malformed length (e.g. 2 words = 128 hex chars) or non-zero trailing words -> fail-closed
    assert decode_vtoken_borrow_balance_stored("0x" + "0" * 128)["is_valid"] is False
    assert decode_vtoken_borrow_balance_stored("0x" + "0" * 128)["reason"] == "MALFORMED_BORROW_RESPONSE"
    bad_96b = "0x" + ("0" * 63 + "1") + ("0" * 63 + "1") + ("0" * 64)
    assert decode_vtoken_borrow_balance_stored(bad_96b)["is_valid"] is False

    # ALTA-2 regression: coherence gate blocks astronomical/incoherent debt (e.g. 1.4489e159)
    incoherent = decode_compound_account_liquidity(real_comptroller_hex, borrower, "Venus_Core_BSC", measured_debt_usd=1.4489e159)
    assert incoherent["is_valid"] is False
    assert incoherent["reason"] == "DEBT_MEASUREMENT_INCOHERENTE"


def test_08_bot1_flash_loan_liquidation_profitable():
    sim = ProfitSimulator({"min_net_profit_usd": 1.50, "max_flash_loan_usd": 250000.0, "default_gas_limit": 450000})
    pos = {"is_valid": True, "is_liquidatable": True, "borrower": "0xdEaD", "health_factor": 0.92, "total_collateral_usd": 20000.0, "total_debt_usd": 16000.0}
    mkt = {"market_id": "Venus_Core_BSC", "chain": "BNB", "close_factor": 0.50, "liquidation_bonus": 0.10, "flash_loan_fee_rate": 0.0005, "dex_swap_fee_rate": 0.0005}
    res = sim.evaluate_liquidation(pos, mkt, 720.0, 1.0, oracle_is_valid=True, pool_in_range_liquidity=5 * (10 ** 21))
    assert res["would_execute_atomic"] is True
    assert res["net_profit_usd"] > 750.0


def test_09_bot1_flash_loan_liquidation_reverts_dust():
    sim = ProfitSimulator({"min_net_profit_usd": 1.50})
    pos = {"is_valid": True, "is_liquidatable": True, "borrower": "0x01", "health_factor": 0.97, "total_collateral_usd": 15.0, "total_debt_usd": 10.0}
    mkt = {"market_id": "Venus_Core_BSC", "chain": "BNB", "close_factor": 0.50, "liquidation_bonus": 0.05}
    res = sim.evaluate_liquidation(pos, mkt, 720.0, 3.0, oracle_is_valid=True, pool_in_range_liquidity=5 * (10 ** 21))
    assert res["would_execute_atomic"] is False


def test_10_bot1_rejects_healthy_and_insolvent_positions():
    sim = ProfitSimulator({"min_net_profit_usd": 1.50})
    mkt = {"market_id": "Kinza_Finance_BSC", "chain": "BNB", "close_factor": 0.50, "liquidation_bonus": 0.08}
    assert sim.evaluate_liquidation({"is_valid": True, "is_liquidatable": False, "health_factor": 1.05, "total_collateral_usd": 10000.0, "total_debt_usd": 8000.0}, mkt, 720.0, 1.0)["would_execute_atomic"] is False
    assert sim.evaluate_liquidation({"is_valid": True, "is_liquidatable": True, "health_factor": 0.40, "total_collateral_usd": 3000.0, "total_debt_usd": 10000.0}, mkt, 720.0, 1.0)["would_execute_atomic"] is False


def test_11_bot1_fail_closed_on_invalid_oracle_or_zero_liquidity():
    sim = ProfitSimulator({"min_net_profit_usd": 1.50})
    mkt = {"market_id": "Venus_Core_BSC", "chain": "BNB"}
    pos = {"is_valid": True, "is_liquidatable": True, "health_factor": 0.88, "total_collateral_usd": 50000.0, "total_debt_usd": 40000.0}
    assert sim.evaluate_liquidation(pos, mkt, 720.0, 1.0, oracle_is_valid=False)["status"] == "ABORTED_ORACLE_OR_GAS_INVALID"
    assert sim.evaluate_liquidation(pos, mkt, 720.0, 1.0, oracle_is_valid=True, pool_in_range_liquidity=0)["status"] == "ABORTED_ZERO_DEX_LIQUIDITY"


def test_12_shadow_logger_sha256_attestation_and_test_isolation():
    with tempfile.TemporaryDirectory() as tmpdir:
        logger = ShadowLogger(liquidations_path=os.path.join(tmpdir, "l.jsonl"), telemetry_path=os.path.join(tmpdir, "t.jsonl"))
        rec_t = logger.log_liquidation_evaluation({"borrower": "0xt", "net_profit_usd": 25.0}, is_test=True)
        rec_l = logger.log_liquidation_evaluation({"borrower": "0xl", "net_profit_usd": 80.0}, is_test=False)
        assert rec_t["record_sha256"] == compute_record_sha256(rec_t)
        assert rec_l["record_sha256"] == compute_record_sha256(rec_l)
        assert len(logger.read_recent_liquidations(include_tests=False)) == 1


def test_13_uniswap_v2_reserves_decoding():
    raw = _pack_words(28785000 * (10 ** 18), 39500 * (10 ** 18), 1760570000)
    res = decode_uniswap_v2_reserves(raw)
    assert res["is_valid"] is True
    assert res["spot_price_usd"] == pytest.approx(28785000.0 / 39500.0, rel=1e-4)


def test_14_bot2_dex_flash_swap_arbitrage():
    engine = UnifiedCommissionEngine({"min_net_profit_usd": 1.50})
    arb_ok = engine.evaluate_dex_flash_arbitrage(
        pair_symbol="WBNB/USDT", chain="BNB",
        pool_a_name="PancakeSwap_V3", pool_a_price_usd=720.0, pool_a_fee_rate=0.0005,
        pool_b_name="PancakeSwap_V2", pool_b_price_usd=726.0, pool_b_fee_rate=0.0025,
        trade_notional_usd=5000.0, native_token_price_usd=720.0, gas_price_gwei=0.05
    )
    assert arb_ok["would_execute_atomic"] is True
    assert arb_ok["strategy_id"] == "BOT_2_DEX_FLASH_ARBITRAGE"
    assert arb_ok["net_profit_usd"] > 20.0


def test_15_bot3_lista_dao_auction_kick_bounty():
    engine = UnifiedCommissionEngine({"min_net_profit_usd": 1.50})
    # Vault below MCR (5200 / 4900 = 1.0612 < 1.10 MCR) -> triggers kick bounty ($15 flat + 1% of $4,900 = $64 gross)
    kick_ok = engine.evaluate_auction_kick_bounty(
        protocol="Lista_DAO_lisUSD", chain="BNB", vault_owner="0xbeef",
        collateral_usd=5200.0, debt_usd=4900.0, min_collateral_ratio=1.10,
        flat_kick_tip_usd=15.0, percentage_kick_bonus_rate=0.01,
        native_token_price_usd=727.0, gas_price_gwei=0.05
    )
    assert kick_ok["would_execute_atomic"] is True
    assert kick_ok["strategy_id"] == "BOT_3_AUCTION_KICK_KEEPER"
    assert kick_ok["net_profit_usd"] == pytest.approx(64.0 - kick_ok["gas_cost_usd"], abs=1e-3)

    # Healthy vault (ICR = 1.50 >= 1.10) -> skipped
    kick_skip = engine.evaluate_auction_kick_bounty(
        protocol="Lista_DAO_lisUSD", chain="BNB", vault_owner="0xsafe",
        collateral_usd=7500.0, debt_usd=5000.0, min_collateral_ratio=1.10,
        flat_kick_tip_usd=15.0, percentage_kick_bonus_rate=0.01,
        native_token_price_usd=727.0, gas_price_gwei=0.05
    )
    assert kick_skip["would_execute_atomic"] is False
    assert kick_skip["status"] == "SKIPPED_VAULT_ABOVE_MCR"


def test_16_bot4_beefy_vault_harvest_keeper():
    engine = UnifiedCommissionEngine({"min_net_profit_usd": 1.50})
    res = engine.evaluate_vault_harvest_keeper(
        vault_id="Beefy_Venus_USDT_Vault", chain="BNB",
        pending_rewards_usd=1200.0, call_fee_rate=0.005,
        native_token_price_usd=727.0, gas_price_gwei=0.05
    )
    assert res["would_execute_atomic"] is True
    assert res["strategy_id"] == "BOT_4_VAULT_HARVEST_KEEPER"
    assert res["net_profit_usd"] > 5.90


def test_17_bot5_perp_order_and_liquidation_keeper():
    engine = UnifiedCommissionEngine({"min_net_profit_usd": 1.50})
    # Triggered LONG liquidation when oracle price ($725) <= trigger price ($730)
    perp_ok = engine.evaluate_perp_order_keeper(
        protocol="ApolloX_V2_BSC", chain="BNB", order_id="ORD-1",
        order_type="LIQUIDATION", direction="LONG",
        trigger_price_usd=730.0, current_oracle_price_usd=725.0,
        position_size_usd=15000.0, execution_fee_usd=2.50, keeper_bonus_rate=0.002,
        native_token_price_usd=725.0, gas_price_gwei=0.05
    )
    assert perp_ok["would_execute_atomic"] is True
    assert perp_ok["strategy_id"] == "BOT_5_PERP_ORDER_KEEPER"
    assert perp_ok["net_profit_usd"] > 32.0

    # Not triggered when LONG price ($740) > trigger price ($730)
    perp_wait = engine.evaluate_perp_order_keeper(
        protocol="ApolloX_V2_BSC", chain="BNB", order_id="ORD-2",
        order_type="LIQUIDATION", direction="LONG",
        trigger_price_usd=730.0, current_oracle_price_usd=740.0,
        position_size_usd=15000.0, execution_fee_usd=2.50, keeper_bonus_rate=0.002,
        native_token_price_usd=740.0, gas_price_gwei=0.05
    )
    assert perp_wait["would_execute_atomic"] is False
    assert perp_wait["status"] == "SKIPPED_TRIGGER_PRICE_NOT_REACHED"


def test_18_central_profit_priority_queue_ranks_all_5_bots():
    opps = [
        {"strategy_id": "BOT_4_VAULT_HARVEST_KEEPER", "would_execute_atomic": True, "net_profit_usd": 9.24},
        {"strategy_id": "BOT_1_FLASH_LIQUIDATION", "would_execute_atomic": True, "net_profit_usd": 587.04},
        {"strategy_id": "BOT_2_DEX_FLASH_ARBITRAGE", "would_execute_atomic": True, "net_profit_usd": 29.59},
        {"strategy_id": "BOT_3_AUCTION_KICK_KEEPER", "would_execute_atomic": True, "net_profit_usd": 63.99},
        {"strategy_id": "BOT_5_PERP_ORDER_KEEPER", "would_execute_atomic": True, "net_profit_usd": 32.49}
    ]
    ranked = UnifiedCommissionEngine.rank_unified_opportunities(opps)
    assert len(ranked) == 5
    assert [r["strategy_id"] for r in ranked] == [
        "BOT_1_FLASH_LIQUIDATION",
        "BOT_3_AUCTION_KICK_KEEPER",
        "BOT_5_PERP_ORDER_KEEPER",
        "BOT_2_DEX_FLASH_ARBITRAGE",
        "BOT_4_VAULT_HARVEST_KEEPER"
    ]
