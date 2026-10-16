"""
Unit and Integration Test Suite for Flash Loan Liquidator (Phase 1 - Shadow Mode).
Verifies fail-closed Chainlink decoding, Aave V3 & Venus/Compound position health math,
net profit calculation after Flash Loan fees/slippage/gas, and SHA-256 JSONL logging.
"""

import os
import sys
import json
import tempfile
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from oracle_watcher import (
    decode_chainlink_latest_round_data,
    decode_uniswap_v3_slot0,
    MultiChainRPCClient
)
from position_indexer import (
    encode_address_param,
    decode_aave_v3_user_account_data,
    decode_compound_account_liquidity
)
from profit_simulator import ProfitSimulator
from shadow_logger import ShadowLogger, compute_record_sha256


def _pack_words(*words: int) -> str:
    """Helper to pack 256-bit unsigned/signed integers into a 0x-prefixed hex payload."""
    hex_chunks = []
    for w in words:
        if w < 0:
            w = (1 << 256) + w
        hex_chunks.append(f"{w:064x}")
    return "0x" + "".join(hex_chunks)


def test_01_chainlink_valid_round_decoding():
    """Verifies accurate decoding of a valid Chainlink latestRoundData response."""
    now_ts = 1760570000
    raw = _pack_words(
        18446744073709562001,  # roundId
        72880824556,           # answer ($728.80824556 at 8 decimals)
        now_ts - 45,           # startedAt
        now_ts - 40,           # updatedAt
        18446744073709562001   # answeredInRound
    )
    res = decode_chainlink_latest_round_data(raw, decimals=8, max_staleness_seconds=3600, now_ts=now_ts)
    assert res["is_valid"] is True
    assert res["reason"] == "OK"
    assert res["price_usd"] == pytest.approx(728.80824556)
    assert res["age_seconds"] == 40.0


def test_02_chainlink_fail_closed_negative_or_zero_answer():
    """Rejects zero or negative oracle answers (two's complement int256)."""
    now_ts = 1760570000
    raw_zero = _pack_words(100, 0, now_ts - 10, now_ts - 5, 100)
    res_zero = decode_chainlink_latest_round_data(raw_zero, decimals=8, now_ts=now_ts)
    assert res_zero["is_valid"] is False
    assert res_zero["reason"] == "NON_POSITIVE_ORACLE_ANSWER"

    raw_neg = _pack_words(100, -50000000, now_ts - 10, now_ts - 5, 100)
    res_neg = decode_chainlink_latest_round_data(raw_neg, decimals=8, now_ts=now_ts)
    assert res_neg["is_valid"] is False
    assert res_neg["reason"] == "NON_POSITIVE_ORACLE_ANSWER"


def test_03_chainlink_fail_closed_stale_timestamp_and_incomplete_round():
    """Rejects stale timestamps and answeredInRound < roundId."""
    now_ts = 1760570000
    raw_stale = _pack_words(105, 60000000000, now_ts - 7200, now_ts - 7200, 105)
    res_stale = decode_chainlink_latest_round_data(raw_stale, decimals=8, max_staleness_seconds=3600, now_ts=now_ts)
    assert res_stale["is_valid"] is False
    assert res_stale["reason"] == "STALE_ORACLE_TIMESTAMP"

    raw_incomplete = _pack_words(105, 60000000000, now_ts - 30, now_ts - 20, 104)
    res_inc = decode_chainlink_latest_round_data(raw_incomplete, decimals=8, max_staleness_seconds=3600, now_ts=now_ts)
    assert res_inc["is_valid"] is False
    assert res_inc["reason"] == "STALE_ANSWERED_IN_ROUND"


def test_04_uniswap_v3_slot0_and_liquidity_decoding():
    """Verifies PancakeSwap V3 / Uniswap V3 slot0() and liquidity() decoding."""
    sqrt_price_x96 = 1 << 96  # Ratio = 1.0
    raw_slot0 = _pack_words(sqrt_price_x96, -120, 0, 1, 1, 0, 1)
    raw_liq = hex(5000000000000000000000)
    res = decode_uniswap_v3_slot0(raw_slot0, raw_liq)
    assert res["is_valid"] is True
    assert res["spot_price"] == pytest.approx(1.0)
    assert res["tick"] == -120
    assert res["in_range_liquidity"] == 5000000000000000000000


def test_05_rpc_client_blocks_write_methods():
    """Enforces Shadow Mode read-only guarantee by blocking eth_sendRawTransaction."""
    client = MultiChainRPCClient({"BNB": ["https://bsc-rpc.publicnode.com"]})
    with pytest.raises(PermissionError, match="Shadow Mode violation"):
        client.call_rpc("BNB", "eth_sendRawTransaction", ["0xdeadbeef"])


def test_06_aave_v3_user_account_decoding_underwater_and_healthy():
    """Verifies Aave V3 / Kinza 6-tuple decoding for both healthy and underwater borrowers."""
    borrower = "0x955b7a7ddcbf443d0e6b899ba1aba71f7413272f"
    # Underwater: $10,000 collateral (8 dec), $8,500 debt (8 dec), HF = 0.94 (18 dec)
    raw_underwater = _pack_words(
        10000 * (10 ** 8),
        8500 * (10 ** 8),
        0,
        8000,
        7500,
        int(0.94 * (10 ** 18))
    )
    pos_u = decode_aave_v3_user_account_data(raw_underwater, borrower, "Kinza_Finance_BSC")
    assert pos_u["is_valid"] is True
    assert pos_u["is_liquidatable"] is True
    assert pos_u["health_factor"] == pytest.approx(0.94)
    assert pos_u["total_collateral_usd"] == pytest.approx(10000.0)
    assert pos_u["total_debt_usd"] == pytest.approx(8500.0)

    # Healthy: HF = 1.45
    raw_healthy = _pack_words(
        15000 * (10 ** 8),
        8500 * (10 ** 8),
        2000 * (10 ** 8),
        8000,
        7500,
        int(1.45 * (10 ** 18))
    )
    pos_h = decode_aave_v3_user_account_data(raw_healthy, borrower, "Kinza_Finance_BSC")
    assert pos_h["is_valid"] is True
    assert pos_h["is_liquidatable"] is False
    assert pos_h["health_factor"] == pytest.approx(1.45)


def test_07_compound_venus_account_liquidity_decoding_and_error_gate():
    """Verifies Venus getAccountLiquidity decoding and Comptroller non-zero error rejection."""
    borrower = "0x955b7a7ddcbf443d0e6b899ba1aba71f7413272f"
    # Non-zero error code (e.g., error = 3) must fail-closed
    raw_err = _pack_words(3, 0, 500 * (10 ** 18))
    res_err = decode_compound_account_liquidity(raw_err, borrower, "Venus_Core_BSC")
    assert res_err["is_valid"] is False
    assert res_err["reason"] == "COMPTROLLER_ERROR_3"

    # Underwater account: error = 0, liquidity = 0, shortfall = $1,200
    raw_shortfall = _pack_words(0, 0, 1200 * (10 ** 18))
    res_sf = decode_compound_account_liquidity(raw_shortfall, borrower, "Venus_Core_BSC", measured_debt_usd=10000.0)
    assert res_sf["is_valid"] is True
    assert res_sf["is_liquidatable"] is True
    assert res_sf["shortfall_usd"] == pytest.approx(1200.0)
    assert res_sf["health_factor"] < 1.0


def test_08_profit_simulator_profitable_atomic_liquidation():
    """Verifies accurate net profit calculation for a profitable underwater position."""
    sim = ProfitSimulator({"min_net_profit_usd": 1.50, "max_flash_loan_usd": 250000.0, "default_gas_limit": 450000})
    position = {
        "is_valid": True,
        "is_liquidatable": True,
        "borrower": "0x000000000000000000000000000000000000dEaD",
        "health_factor": 0.92,
        "total_collateral_usd": 20000.0,
        "total_debt_usd": 16000.0
    }
    market = {
        "market_id": "Venus_Core_BSC",
        "chain": "BNB",
        "close_factor": 0.50,
        "liquidation_bonus": 0.10,
        "flash_loan_fee_rate": 0.0005,
        "dex_swap_fee_rate": 0.0005
    }
    res = sim.evaluate_liquidation(
        position=position,
        market=market,
        native_token_price_usd=720.0,
        gas_price_gwei=1.0,
        oracle_is_valid=True,
        pool_in_range_liquidity=5 * (10 ** 21)
    )
    assert res["would_execute_atomic"] is True
    assert res["status"] == "PROFITABLE_OPPORTUNITY_LOGGED"
    assert res["debt_to_cover_usd"] == pytest.approx(8000.0)
    assert res["gross_bonus_usd"] == pytest.approx(800.0)
    assert res["flash_loan_fee_usd"] == pytest.approx(4.0)
    assert res["net_profit_usd"] > 750.0


def test_09_profit_simulator_reverts_unprofitable_dust_position():
    """Verifies atomic revert simulation when gas + fees exceed the liquidation bonus."""
    sim = ProfitSimulator({"min_net_profit_usd": 1.50, "max_flash_loan_usd": 250000.0, "default_gas_limit": 450000})
    position = {
        "is_valid": True,
        "is_liquidatable": True,
        "borrower": "0x0000000000000000000000000000000000000001",
        "health_factor": 0.97,
        "total_collateral_usd": 15.0,
        "total_debt_usd": 10.0
    }
    market = {
        "market_id": "Venus_Core_BSC",
        "chain": "BNB",
        "close_factor": 0.50,
        "liquidation_bonus": 0.05,
        "flash_loan_fee_rate": 0.0005,
        "dex_swap_fee_rate": 0.0005
    }
    # Debt covered = $5.0, 5% gross bonus = $0.25 < $1.50 min threshold
    res = sim.evaluate_liquidation(
        position=position,
        market=market,
        native_token_price_usd=720.0,
        gas_price_gwei=3.0,
        oracle_is_valid=True,
        pool_in_range_liquidity=5 * (10 ** 21)
    )
    assert res["would_execute_atomic"] is False
    assert res["status"] == "REVERTED_NET_PROFIT_BELOW_THRESHOLD"
    assert res["net_profit_usd"] < 1.50


def test_10_profit_simulator_rejects_healthy_and_insolvent_positions():
    """Rejects healthy (HF >= 1.0) and deeply insolvent (collateral < debt covered) positions."""
    sim = ProfitSimulator({"min_net_profit_usd": 1.50})
    market = {"market_id": "Kinza_Finance_BSC", "chain": "BNB", "close_factor": 0.50, "liquidation_bonus": 0.08}

    healthy_pos = {
        "is_valid": True,
        "is_liquidatable": False,
        "borrower": "0x1111111111111111111111111111111111111111",
        "health_factor": 1.05,
        "total_collateral_usd": 10000.0,
        "total_debt_usd": 8000.0
    }
    res_h = sim.evaluate_liquidation(healthy_pos, market, 720.0, 1.0, oracle_is_valid=True)
    assert res_h["would_execute_atomic"] is False
    assert res_h["status"] == "SKIPPED_HEALTHY_POSITION"

    # Deeply insolvent position where collateral ($3,000) < debt to cover ($5,000)
    insolvent_pos = {
        "is_valid": True,
        "is_liquidatable": True,
        "borrower": "0x2222222222222222222222222222222222222222",
        "health_factor": 0.40,
        "total_collateral_usd": 3000.0,
        "total_debt_usd": 10000.0
    }
    res_ins = sim.evaluate_liquidation(insolvent_pos, market, 720.0, 1.0, oracle_is_valid=True)
    assert res_ins["would_execute_atomic"] is False
    assert res_ins["net_profit_usd"] < 0.0


def test_11_profit_simulator_fail_closed_on_invalid_oracle_or_zero_liquidity():
    """Blocks execution if Chainlink oracle is stale/invalid or DEX pool liquidity is zero."""
    sim = ProfitSimulator({"min_net_profit_usd": 1.50})
    market = {"market_id": "Venus_Core_BSC", "chain": "BNB", "close_factor": 0.50, "liquidation_bonus": 0.10}
    pos = {
        "is_valid": True,
        "is_liquidatable": True,
        "borrower": "0x3333333333333333333333333333333333333333",
        "health_factor": 0.88,
        "total_collateral_usd": 50000.0,
        "total_debt_usd": 40000.0
    }

    res_bad_oracle = sim.evaluate_liquidation(pos, market, 720.0, 1.0, oracle_is_valid=False)
    assert res_bad_oracle["would_execute_atomic"] is False
    assert res_bad_oracle["status"] == "ABORTED_ORACLE_OR_GAS_INVALID"

    res_zero_liq = sim.evaluate_liquidation(
        pos, market, 720.0, 1.0, oracle_is_valid=True, pool_in_range_liquidity=0
    )
    assert res_zero_liq["would_execute_atomic"] is False
    assert res_zero_liq["status"] == "ABORTED_ZERO_DEX_LIQUIDITY"


def test_12_shadow_logger_sha256_attestation_and_test_isolation():
    """Verifies JSONL append persistence, SHA-256 record integrity, and test flag filtering."""
    with tempfile.TemporaryDirectory() as tmpdir:
        liq_log = os.path.join(tmpdir, "liq.jsonl")
        tel_log = os.path.join(tmpdir, "tel.jsonl")
        logger = ShadowLogger(liquidations_path=liq_log, telemetry_path=tel_log)

        rec_test = logger.log_liquidation_evaluation({"borrower": "0xtest", "net_profit_usd": 25.0}, is_test=True)
        rec_live = logger.log_liquidation_evaluation({"borrower": "0xlive", "net_profit_usd": 80.0}, is_test=False)

        assert rec_test["record_sha256"] == compute_record_sha256(rec_test)
        assert rec_live["record_sha256"] == compute_record_sha256(rec_live)

        live_only = logger.read_recent_liquidations(include_tests=False)
        assert len(live_only) == 1
        assert live_only[0]["borrower"] == "0xlive"

        all_recs = logger.read_recent_liquidations(include_tests=True)
        assert len(all_recs) == 2
