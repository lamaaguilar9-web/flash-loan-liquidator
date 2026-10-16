"""
Flash Loan Liquidator (Module C) - Phase 1 Shadow Mode
Append-Only Shadow Logger with SHA-256 Record Attestation.

Persists read-only telemetry cycles and simulated liquidation evaluations to JSONL
with explicit separation between live on-chain observations (is_test=False)
and deterministic stress-test simulations (is_test=True).
"""

import os
import json
import time
import hashlib
from datetime import datetime, timezone
from typing import Dict, Any, List


DEFAULT_LIQUIDATIONS_LOG = os.path.join(os.path.dirname(__file__), "data", "shadow_liquidations.jsonl")
DEFAULT_TELEMETRY_LOG = os.path.join(os.path.dirname(__file__), "data", "shadow_telemetry.jsonl")


def compute_record_sha256(payload: Dict[str, Any]) -> str:
    """Computes canonical SHA-256 hex digest over sorted JSON keys (excluding record_sha256)."""
    clean = {k: v for k, v in payload.items() if k != "record_sha256"}
    serialized = json.dumps(clean, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


class ShadowLogger:
    """Append-only JSONL logger for Shadow Mode telemetry and liquidation opportunities."""

    def __init__(
        self,
        liquidations_path: str = DEFAULT_LIQUIDATIONS_LOG,
        telemetry_path: str = DEFAULT_TELEMETRY_LOG
    ):
        self.liquidations_path = liquidations_path
        self.telemetry_path = telemetry_path
        os.makedirs(os.path.dirname(self.liquidations_path), exist_ok=True)
        os.makedirs(os.path.dirname(self.telemetry_path), exist_ok=True)

    def log_liquidation_evaluation(self, evaluation: Dict[str, Any], is_test: bool = False) -> Dict[str, Any]:
        """Appends a liquidation profit evaluation record with SHA-256 attestation."""
        now_ts = time.time()
        record = {
            "timestamp": round(now_ts, 3),
            "iso_time": datetime.fromtimestamp(now_ts, tz=timezone.utc).isoformat(),
            "mode": "MODO_SOMBRA_SOLO_LECTURA",
            "is_test": bool(is_test),
            **evaluation
        }
        record["record_sha256"] = compute_record_sha256(record)
        with open(self.liquidations_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        return record

    def log_telemetry_cycle(self, cycle_summary: Dict[str, Any], is_test: bool = False) -> Dict[str, Any]:
        """Appends a full on-chain scan cycle summary to shadow_telemetry.jsonl."""
        now_ts = time.time()
        record = {
            "timestamp": round(now_ts, 3),
            "iso_time": datetime.fromtimestamp(now_ts, tz=timezone.utc).isoformat(),
            "mode": "MODO_SOMBRA_SOLO_LECTURA",
            "is_test": bool(is_test),
            **cycle_summary
        }
        record["record_sha256"] = compute_record_sha256(record)
        with open(self.telemetry_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        return record

    def read_recent_liquidations(self, limit: int = 50, include_tests: bool = False) -> List[Dict[str, Any]]:
        """Reads recent liquidation evaluation records from JSONL."""
        if not os.path.exists(self.liquidations_path):
            return []
        records: List[Dict[str, Any]] = []
        with open(self.liquidations_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                    if not include_tests and item.get("is_test", False):
                        continue
                    records.append(item)
                except Exception:
                    continue
        return records[-limit:]
