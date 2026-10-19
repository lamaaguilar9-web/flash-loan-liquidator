"""
Flash Loan Liquidator (Module C) - Phase 1 Shadow Mode
Oracle Watcher & DEX Pool Depth Verifier (Fail-Closed).

Queries real on-chain Chainlink AggregatorV3 feeds (latestRoundData / decimals)
and PancakeSwap V3 pool state (slot0 / liquidity) via JSON-RPC eth_call.
Enforces strict fail-closed validation against stale rounds, negative/zero answers,
and incomplete round updates.
"""

import time
import logging
from typing import Dict, Any, List, Optional, Tuple
import requests

logger = logging.getLogger("OracleWatcher")

# Canonical EVM Function Selectors (keccak256[:4])
SELECTOR_LATEST_ROUND_DATA = "0xfeaf968c"  # latestRoundData() -> (uint80,int256,uint256,uint256,uint80)
SELECTOR_DECIMALS = "0x313ce567"           # decimals() -> uint8
SELECTOR_SLOT0 = "0x3850c7bd"              # slot0() -> (uint160 sqrtPriceX96, int24 tick, ...)
SELECTOR_LIQUIDITY = "0x1a686502"          # liquidity() -> uint128


def decode_signed_256(word_hex: str) -> int:
    """Decodes a 64-hex-character EVM word as signed int256 (two's complement)."""
    val = int(word_hex, 16)
    if val >= (1 << 255):
        val -= (1 << 256)
    return val


def decode_chainlink_latest_round_data(
    raw_hex: str,
    decimals: int = 8,
    max_staleness_seconds: int = 3600,
    now_ts: Optional[ float ] = None
) -> Dict[str, Any]:
    """
    Decodes Chainlink AggregatorV3Interface.latestRoundData() raw hex response
    and applies fail-closed integrity gates.
    """
    if now_ts is None:
        now_ts = time.time()

    if not raw_hex or not isinstance(raw_hex, str) or not raw_hex.startswith("0x"):
        return {
            "is_valid": False,
            "reason": "EMPTY_OR_MALFORMED_HEX",
            "price_usd": None
        }

    payload = raw_hex[2:]
    if len(payload) < 320:  # 5 words * 64 hex chars = 320 chars
        return {
            "is_valid": False,
            "reason": "TRUNCATED_ROUND_DATA",
            "price_usd": None
        }

    try:
        round_id = int(payload[0:64], 16)
        answer = decode_signed_256(payload[64:128])
        started_at = int(payload[128:192], 16)
        updated_at = int(payload[192:256], 16)
        answered_in_round = int(payload[256:320], 16)
    except ValueError:
        return {
            "is_valid": False,
            "reason": "HEX_PARSE_ERROR",
            "price_usd": None
        }

    # Gate 1: Positive non-zero price
    if answer <= 0:
        return {
            "is_valid": False,
            "reason": "NON_POSITIVE_ORACLE_ANSWER",
            "round_id": round_id,
            "raw_answer": answer,
            "price_usd": None
        }

    # Gate 2: Round completeness (answeredInRound >= roundId)
    if answered_in_round < round_id or round_id == 0:
        return {
            "is_valid": False,
            "reason": "STALE_ANSWERED_IN_ROUND",
            "round_id": round_id,
            "answered_in_round": answered_in_round,
            "price_usd": None
        }

    # Gate 3: Timestamp freshness
    if updated_at == 0:
        return {
            "is_valid": False,
            "reason": "ZERO_UPDATED_AT_TIMESTAMP",
            "round_id": round_id,
            "price_usd": None
        }

    age_seconds = max(0.0, float(now_ts) - float(updated_at))
    if max_staleness_seconds > 0 and age_seconds > max_staleness_seconds:
        return {
            "is_valid": False,
            "reason": "STALE_ORACLE_TIMESTAMP",
            "round_id": round_id,
            "updated_at": updated_at,
            "age_seconds": round(age_seconds, 2),
            "max_staleness_seconds": max_staleness_seconds,
            "price_usd": round(answer / (10 ** decimals), 8)
        }

    price_usd = round(answer / (10 ** decimals), 8)
    return {
        "is_valid": True,
        "reason": "OK",
        "round_id": str(round_id),
        "raw_answer": answer,
        "decimals": decimals,
        "price_usd": price_usd,
        "started_at": started_at,
        "updated_at": updated_at,
        "age_seconds": round(age_seconds, 2)
    }


def decode_uniswap_v3_slot0(raw_slot0_hex: str, raw_liquidity_hex: Optional[str] = None) -> Dict[str, Any]:
    """
    Decodes PancakeSwap V3 / Uniswap V3 slot0() and liquidity() responses.
    """
    if not raw_slot0_hex or not raw_slot0_hex.startswith("0x") or len(raw_slot0_hex) < 130:
        return {"is_valid": False, "reason": "INVALID_SLOT0_HEX"}

    payload = raw_slot0_hex[2:]
    sqrt_price_x96 = int(payload[0:64], 16)
    tick_raw = decode_signed_256(payload[64:128])
    if sqrt_price_x96 <= 0:
        return {"is_valid": False, "reason": "ZERO_SQRT_PRICE"}

    # Spot price from sqrtPriceX96: (sqrtPriceX96 / 2^96)^2
    price_ratio = (sqrt_price_x96 / (2 ** 96)) ** 2
    in_range_liquidity = 0
    if raw_liquidity_hex and raw_liquidity_hex.startswith("0x") and len(raw_liquidity_hex) > 2:
        in_range_liquidity = int(raw_liquidity_hex, 16)

    return {
        "is_valid": True,
        "sqrt_price_x96": sqrt_price_x96,
        "tick": tick_raw,
        "spot_price": round(price_ratio, 6),
        "in_range_liquidity": in_range_liquidity
    }


class WriteMethodForbiddenError(PermissionError):
    """Raised when a state-changing JSON-RPC method is attempted in Shadow Mode."""


class MultiChainRPCClient:
    """Failover JSON-RPC client strictly confined to read-only calls."""
    ALLOWED_METHODS = {
        "eth_blockNumber",
        "eth_gasPrice",
        "eth_call",
        "eth_getLogs",
        "eth_getBlockByNumber"
    }

    def __init__(self, rpc_endpoints: Dict[str, List[str]], timeout: int = 5):
        self.rpc_endpoints = rpc_endpoints
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json"})

    def call_rpc(self, chain: str, method: str, params: list) -> Tuple[Optional[Any], float, Optional[str]]:
        """Executes a read-only JSON-RPC call with automatic endpoint failover."""
        if method not in self.ALLOWED_METHODS:
            raise WriteMethodForbiddenError(f"Shadow Mode violation: RPC method '{method}' is prohibited.")

        urls = self.rpc_endpoints.get(chain, [])
        for url in urls:
            try:
                t0 = time.perf_counter()
                resp = self.session.post(
                    url,
                    json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                    timeout=self.timeout
                )
                latency_ms = round((time.perf_counter() - t0) * 1000, 2)
                if resp.status_code == 200:
                    body = resp.json()
                    if "result" in body and body["result"] is not None:
                        return body["result"], latency_ms, url
            except Exception as exc:
                logger.debug(f"[{chain}] RPC failover from {url}: {exc}")
                continue
        return None, 999.0, None

    def eth_call(self, chain: str, to_addr: str, data: str, block: str = "latest") -> Optional[str]:
        res, _, _ = self.call_rpc(chain, "eth_call", [{"to": to_addr, "data": data}, block])
        return res if isinstance(res, str) else None

    def get_chain_state(self, chain: str) -> Dict[str, Any]:
        blk_hex, lat_ms, active_rpc = self.call_rpc(chain, "eth_blockNumber", [])
        gas_hex, _, _ = self.call_rpc(chain, "eth_gasPrice", [])
        block_number = int(blk_hex, 16) if isinstance(blk_hex, str) and blk_hex.startswith("0x") else None
        gas_price_gwei = round(int(gas_hex, 16) / 1e9, 4) if isinstance(gas_hex, str) and gas_hex.startswith("0x") else None
        return {
            "chain": chain,
            "block_number": block_number,
            "gas_price_gwei": gas_price_gwei,
            "rpc_latency_ms": lat_ms if block_number else None,
            "active_rpc": active_rpc
        }


class OracleWatcher:
    """Watches Chainlink price feeds and DEX liquidity pools in Shadow Mode."""

    def __init__(self, rpc_client: MultiChainRPCClient, config: Dict[str, Any]):
        self.rpc = rpc_client
        self.config = config
        self.feeds_config = config.get("chainlink_feeds", {})
        self.dex_config = config.get("dex_liquidity_pools", {})

    def fetch_all_oracles(self, chain: str) -> Dict[str, Dict[str, Any]]:
        """Reads all configured Chainlink feeds for the target chain."""
        chain_feeds = self.feeds_config.get(chain, {})
        results: Dict[str, Dict[str, Any]] = {}
        now_ts = time.time()

        for symbol, meta in chain_feeds.items():
            addr = meta["address"]
            default_dec = int(meta.get("decimals", 8))
            max_stale = int(meta.get("max_staleness_seconds", 3600))

            raw_round = self.rpc.eth_call(chain, addr, SELECTOR_LATEST_ROUND_DATA)
            raw_dec = self.rpc.eth_call(chain, addr, SELECTOR_DECIMALS)
            decimals = default_dec
            if raw_dec and raw_dec.startswith("0x") and len(raw_dec) > 2:
                try:
                    decimals = int(raw_dec, 16)
                except ValueError:
                    decimals = default_dec

            decoded = decode_chainlink_latest_round_data(
                raw_hex=raw_round or "",
                decimals=decimals,
                max_staleness_seconds=max_stale,
                now_ts=now_ts
            )
            decoded["symbol"] = symbol
            decoded["feed_address"] = addr
            decoded["chain"] = chain
            results[symbol] = decoded

        return results

    def fetch_dex_pool_state(self, pool_key: str) -> Dict[str, Any]:
        """Reads real on-chain slot0() and liquidity() from PancakeSwap V3 / Uniswap V3 pool."""
        pool_meta = self.dex_config.get(pool_key)
        if not pool_meta:
            return {"is_valid": False, "reason": "UNKNOWN_POOL_KEY"}

        chain = pool_meta["chain"]
        addr = pool_meta["address"]
        raw_slot0 = self.rpc.eth_call(chain, addr, SELECTOR_SLOT0)
        raw_liq = self.rpc.eth_call(chain, addr, SELECTOR_LIQUIDITY)

        decoded = decode_uniswap_v3_slot0(raw_slot0 or "", raw_liq)
        decoded["pool_key"] = pool_key
        decoded["pool_address"] = addr
        decoded["chain"] = chain
        return decoded
