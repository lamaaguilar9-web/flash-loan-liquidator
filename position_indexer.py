"""
Flash Loan Liquidator (Module C) - Phase 1 Shadow Mode
Position Indexer & On-Chain Account Health Evaluator.

Discovers active borrowers from recent on-chain logs (eth_getLogs) and curated seeds,
and evaluates account health on:
  - Aave V3 / Kinza Finance architecture via getUserAccountData(address)
  - Compound V2 / Venus Protocol architecture via getAccountLiquidity(address)
"""

import logging
from typing import Dict, Any, List, Optional, Set
from oracle_watcher import MultiChainRPCClient

logger = logging.getLogger("PositionIndexer")

# Canonical EVM Selectors
SELECTOR_GET_USER_ACCOUNT_DATA = "0xbf92857c"  # getUserAccountData(address)
SELECTOR_GET_ACCOUNT_LIQUIDITY = "0x5ec88c79"  # getAccountLiquidity(address) -> (uint256 err, uint256 liq, uint256 shortfall)
SELECTOR_BORROW_BALANCE_STORED = "0x95dd9193"  # borrowBalanceStored(address) -> uint256


def encode_address_param(address: str) -> str:
    """Pads a 20-byte EVM hex address to a 32-byte (64-hex-char) ABI word."""
    clean = address.strip().lower()
    if clean.startswith("0x"):
        clean = clean[2:]
    if len(clean) != 40:
        raise ValueError(f"Invalid EVM address length: {address}")
    int(clean, 16)  # Validates hex characters
    return clean.rjust(64, "0")


def decode_aave_v3_user_account_data(raw_hex: str, borrower: str, market_id: str) -> Dict[str, Any]:
    """
    Decodes Aave V3 / Kinza Pool.getUserAccountData(address) 6-word return tuple:
      0: totalCollateralBase (8 decimals USD)
      1: totalDebtBase (8 decimals USD)
      2: availableBorrowsBase (8 decimals USD)
      3: currentLiquidationThreshold (bps, 1e4 = 100%)
      4: ltv (bps, 1e4 = 100%)
      5: healthFactor (18 decimals, 1e18 = 1.0)
    """
    if not raw_hex or not isinstance(raw_hex, str) or not raw_hex.startswith("0x"):
        return {
            "is_valid": False,
            "reason": "EMPTY_AAVE_ACCOUNT_HEX",
            "borrower": borrower,
            "market_id": market_id
        }

    payload = raw_hex[2:]
    if len(payload) < 384:  # 6 * 64 hex chars
        return {
            "is_valid": False,
            "reason": "TRUNCATED_AAVE_ACCOUNT_HEX",
            "borrower": borrower,
            "market_id": market_id
        }

    try:
        total_collateral_base = int(payload[0:64], 16)
        total_debt_base = int(payload[64:128], 16)
        available_borrows_base = int(payload[128:192], 16)
        liq_threshold_bps = int(payload[192:256], 16)
        ltv_bps = int(payload[256:320], 16)
        hf_raw = int(payload[320:384], 16)
    except ValueError:
        return {
            "is_valid": False,
            "reason": "HEX_PARSE_ERROR",
            "borrower": borrower,
            "market_id": market_id
        }

    total_collateral_usd = round(total_collateral_base / 1e8, 4)
    total_debt_usd = round(total_debt_base / 1e8, 4)
    available_borrows_usd = round(available_borrows_base / 1e8, 4)

    if total_debt_base == 0 or hf_raw > (10 ** 30):
        health_factor = 999.0
    else:
        health_factor = round(hf_raw / 1e18, 6)

    is_liquidatable = (total_debt_usd > 0.0) and (health_factor < 1.0)
    shortfall_usd = round(max(0.0, total_debt_usd * (1.0 - health_factor)), 4) if is_liquidatable else 0.0

    return {
        "is_valid": True,
        "reason": "OK",
        "architecture": "aave_v3",
        "market_id": market_id,
        "borrower": borrower,
        "total_collateral_usd": total_collateral_usd,
        "total_debt_usd": total_debt_usd,
        "available_borrows_usd": available_borrows_usd,
        "liquidation_threshold_pct": round(liq_threshold_bps / 100.0, 2),
        "ltv_pct": round(ltv_bps / 100.0, 2),
        "health_factor": health_factor,
        "shortfall_usd": shortfall_usd,
        "is_liquidatable": is_liquidatable
    }


def decode_compound_account_liquidity(
    raw_hex: str,
    borrower: str,
    market_id: str,
    measured_debt_usd: float = 0.0
) -> Dict[str, Any]:
    """
    Decodes Compound V2 / Venus Comptroller.getAccountLiquidity(address):
      0: error (uint256, 0 == NO_ERROR)
      1: liquidity (uint256, 18 decimals USD excess collateral power)
      2: shortfall (uint256, 18 decimals USD underwater shortfall)
    """
    if not raw_hex or not isinstance(raw_hex, str) or not raw_hex.startswith("0x"):
        return {
            "is_valid": False,
            "reason": "EMPTY_COMPOUND_LIQUIDITY_HEX",
            "borrower": borrower,
            "market_id": market_id
        }

    payload = raw_hex[2:]
    if len(payload) < 192:  # 3 * 64 hex chars
        return {
            "is_valid": False,
            "reason": "TRUNCATED_COMPOUND_LIQUIDITY_HEX",
            "borrower": borrower,
            "market_id": market_id
        }

    try:
        err_code = int(payload[0:64], 16)
        liquidity_raw = int(payload[64:128], 16)
        shortfall_raw = int(payload[128:192], 16)
    except ValueError:
        return {
            "is_valid": False,
            "reason": "HEX_PARSE_ERROR",
            "borrower": borrower,
            "market_id": market_id
        }

    # Fail-closed gate: Comptroller non-zero error code
    if err_code != 0:
        return {
            "is_valid": False,
            "reason": f"COMPTROLLER_ERROR_{err_code}",
            "error_code": err_code,
            "borrower": borrower,
            "market_id": market_id
        }

    liquidity_usd = round(liquidity_raw / 1e18, 4)
    shortfall_usd = round(shortfall_raw / 1e18, 4)

    if shortfall_usd > 0.0:
        # Underwater position: shortfall > 0 implies Health Factor < 1.0
        effective_debt_usd = max(measured_debt_usd, round(shortfall_usd * 4.0, 4))
        effective_collateral_usd = round(max(effective_debt_usd - shortfall_usd, shortfall_usd * 1.25), 4)
        health_factor = round(effective_debt_usd / (effective_debt_usd + shortfall_usd), 6)
        is_liquidatable = True
    elif measured_debt_usd > 0.0:
        effective_debt_usd = round(measured_debt_usd, 4)
        effective_collateral_usd = round((measured_debt_usd + liquidity_usd) / 0.80, 4)
        health_factor = round((measured_debt_usd + liquidity_usd) / measured_debt_usd, 6)
        is_liquidatable = False
    elif liquidity_usd > 0.0:
        # Active supplier with either zero debt or unmeasured non-stable debt but positive excess liquidity
        effective_debt_usd = 0.0
        effective_collateral_usd = round(liquidity_usd / 0.80, 4)
        health_factor = 999.0
        is_liquidatable = False
    else:
        effective_debt_usd = 0.0
        effective_collateral_usd = 0.0
        health_factor = 999.0
        is_liquidatable = False

    return {
        "is_valid": True,
        "reason": "OK",
        "architecture": "compound_v2",
        "market_id": market_id,
        "borrower": borrower,
        "liquidity_excess_usd": liquidity_usd,
        "shortfall_usd": shortfall_usd,
        "total_collateral_usd": effective_collateral_usd,
        "total_debt_usd": effective_debt_usd,
        "health_factor": health_factor,
        "is_liquidatable": is_liquidatable
    }


class PositionIndexer:
    """Discovers borrowers and reads real on-chain health factors."""

    def __init__(self, rpc_client: MultiChainRPCClient, config: Dict[str, Any]):
        self.rpc = rpc_client
        self.config = config
        self.watchlist_hf = float(config.get("watchlist_hf_threshold", 1.08))

    def discover_recent_borrowers(self, market: Dict[str, Any], latest_block: Optional[int], max_candidates: int = 8) -> List[str]:
        """
        Combines curated seed borrowers with dynamic borrower discovery from recent on-chain logs.
        """
        candidates: List[str] = []
        seen: Set[str] = set()

        for addr in market.get("seed_borrowers", []):
            low = addr.lower()
            if low not in seen:
                seen.add(low)
                candidates.append(addr)

        if not latest_block or latest_block <= 600:
            return candidates[:max_candidates]

        chain = market["chain"]
        target_addresses = market.get("vtokens", [market["controller_address"]])
        from_blk = hex(max(1, latest_block - 400))
        to_blk = hex(latest_block)

        logs, _, _ = self.rpc.call_rpc(
            chain,
            "eth_getLogs",
            [{"address": target_addresses, "fromBlock": from_blk, "toBlock": to_blk}]
        )

        if isinstance(logs, list):
            for lg in logs:
                if len(candidates) >= max_candidates:
                    break
                data = lg.get("data", "0x")
                topics = lg.get("topics", [])
                # Check first ABI word if it looks like a 20-byte padded address
                if isinstance(data, str) and len(data) >= 66 and data[2:26] == "000000000000000000000000":
                    cand = "0x" + data[26:66].lower()
                    if cand not in seen and int(cand, 16) > 0:
                        seen.add(cand)
                        candidates.append(cand)
                for t in topics[1:]:
                    if isinstance(t, str) and len(t) == 66 and t[:26] == "0x000000000000000000000000":
                        cand = "0x" + t[-40:].lower()
                        if cand not in seen and int(cand, 16) > 0:
                            seen.add(cand)
                            candidates.append(cand)

        return candidates[:max_candidates]

    def evaluate_borrower(self, market: Dict[str, Any], borrower: str) -> Dict[str, Any]:
        """Reads live on-chain position health for a single borrower on the specified market."""
        chain = market["chain"]
        arch = market["architecture"]
        controller = market["controller_address"]
        market_id = market["market_id"]

        try:
            padded_addr = encode_address_param(borrower)
        except ValueError as exc:
            return {
                "is_valid": False,
                "reason": str(exc),
                "borrower": borrower,
                "market_id": market_id
            }

        if arch == "aave_v3":
            raw_hex = self.rpc.eth_call(chain, controller, SELECTOR_GET_USER_ACCOUNT_DATA + padded_addr)
            return decode_aave_v3_user_account_data(raw_hex or "", borrower, market_id)

        if arch == "compound_v2":
            raw_liq = self.rpc.eth_call(chain, controller, SELECTOR_GET_ACCOUNT_LIQUIDITY + padded_addr)
            # Also check vUSDT (first vToken) borrowBalanceStored for accurate stable debt measurement
            measured_debt_usd = 0.0
            vtokens = market.get("vtokens", [])
            if vtokens:
                raw_borrow = self.rpc.eth_call(chain, vtokens[0], SELECTOR_BORROW_BALANCE_STORED + padded_addr)
                if raw_borrow and raw_borrow.startswith("0x") and len(raw_borrow) > 2:
                    try:
                        measured_debt_usd = round(int(raw_borrow, 16) / 1e18, 4)
                    except ValueError:
                        measured_debt_usd = 0.0
            return decode_compound_account_liquidity(raw_liq or "", borrower, market_id, measured_debt_usd)

        return {
            "is_valid": False,
            "reason": f"UNSUPPORTED_ARCHITECTURE_{arch}",
            "borrower": borrower,
            "market_id": market_id
        }
