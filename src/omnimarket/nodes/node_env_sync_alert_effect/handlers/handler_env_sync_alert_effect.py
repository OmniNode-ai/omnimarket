# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerEnvSyncAlertEffect — scan logs and emit environment drift alerts.

ONEX node type: EFFECT_GENERIC — side-effecting, writes to Linear and emits friction events.
Ticket: OMN-12227
"""

from __future__ import annotations

import hashlib
import os
import re
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import yaml

from omnimarket.nodes.node_env_sync_alert_effect.models.model_env_sync_alert_request import (
    ModelEnvSyncAlertRequest,
)
from omnimarket.nodes.node_env_sync_alert_effect.models.model_env_sync_alert_result import (
    ModelEnvSyncAlertResult,
)

_DRIFT_RE = re.compile(
    r"(?:env(?:ironment)?[-_\s]*sync[-_\s]*drift|env[-_\s]*parity|ENV_SYNC_DRIFT)",
    re.IGNORECASE,
)
_KEY_RE = re.compile(r"\b([A-Z][A-Z0-9_]{2,})\b")


class ProtocolLinearAlertAdapter(Protocol):
    """Adapter boundary for Linear ticket creation."""

    def create_ticket(self, payload: dict[str, Any]) -> str: ...


class HandlerEnvSyncAlertEffect:
    """Scan runtime logs for env sync drift and emit friction events."""

    def __init__(
        self,
        linear_adapter: ProtocolLinearAlertAdapter | None = None,
    ) -> None:
        self._linear_adapter = linear_adapter

    def handle(self, request: ModelEnvSyncAlertRequest) -> ModelEnvSyncAlertResult:
        findings = _scan_logs(request.log_paths)
        thresholded = [
            finding
            for finding in findings.values()
            if int(finding["count"]) >= request.alert_threshold
        ]
        friction_dir = _resolve_friction_dir(request.friction_dir)
        friction_events = [
            _emit_friction_event(friction_dir, finding) for finding in thresholded
        ]

        alerts_created = 0
        if request.create_linear_tickets and thresholded:
            if self._linear_adapter is None:
                raise RuntimeError(
                    "linear adapter required when create_linear_tickets is true"
                )
            for finding in thresholded:
                self._linear_adapter.create_ticket(_linear_payload(finding))
                alerts_created += 1

        return ModelEnvSyncAlertResult(
            alerts_created=alerts_created,
            friction_events=friction_events,
        )


def _scan_logs(log_paths: list[str]) -> dict[str, dict[str, Any]]:
    findings: dict[str, dict[str, Any]] = {}
    counts: Counter[str] = Counter()
    examples: dict[str, list[str]] = defaultdict(list)
    log_path_by_signature: dict[str, str] = {}

    for raw_path in log_paths:
        path = Path(raw_path)
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if _DRIFT_RE.search(line) is None:
                continue
            signature = _signature(path, line)
            counts[signature] += 1
            log_path_by_signature[signature] = str(path)
            if len(examples[signature]) < 3:
                examples[signature].append(line.strip())

    for signature, count in sorted(counts.items()):
        findings[signature] = {
            "drift_signature": signature,
            "count": count,
            "log_path": log_path_by_signature[signature],
            "examples": examples[signature],
            "env_keys": _extract_env_keys("\n".join(examples[signature])),
        }
    return findings


def _signature(path: Path, line: str) -> str:
    keys = ",".join(_extract_env_keys(line))
    base = f"{path.name}:{keys or _normalize_line(line)}"
    return hashlib.sha256(base.encode("utf-8")).hexdigest()[:16]


def _extract_env_keys(value: str) -> list[str]:
    ignored = {"ENV_SYNC_DRIFT"}
    return sorted(key for key in set(_KEY_RE.findall(value)) if key not in ignored)


def _normalize_line(line: str) -> str:
    return " ".join(line.strip().split())[:160]


def _resolve_friction_dir(raw: str | None) -> Path:
    if raw:
        return Path(raw)
    state_dir = os.environ.get("ONEX_STATE_DIR")
    if state_dir:
        return Path(state_dir) / "friction"
    omni_home = os.environ.get("OMNI_HOME")
    if omni_home:
        return Path(omni_home) / ".onex_state" / "friction"
    return Path(".onex_state") / "friction"


def _emit_friction_event(friction_dir: Path, finding: dict[str, Any]) -> dict[str, Any]:
    friction_dir.mkdir(parents=True, exist_ok=True)
    occurred_at = datetime.now(tz=UTC).isoformat()
    event = {
        "event_type": "env_sync_drift",
        "occurred_at": occurred_at,
        "severity": "medium",
        "drift_signature": finding["drift_signature"],
        "log_path": finding["log_path"],
        "count": finding["count"],
        "env_keys": finding["env_keys"],
        "examples": finding["examples"],
    }
    path = friction_dir / f"env-sync-drift-{finding['drift_signature']}.yaml"
    path.write_text(yaml.safe_dump(event, sort_keys=True), encoding="utf-8")
    return {**event, "friction_path": str(path)}


def _linear_payload(finding: dict[str, Any]) -> dict[str, Any]:
    keys = ", ".join(finding["env_keys"]) or "unknown env keys"
    return {
        "title": f"Environment sync drift detected: {keys}",
        "description": (
            f"Drift signature: {finding['drift_signature']}\n"
            f"Log path: {finding['log_path']}\n"
            f"Occurrences: {finding['count']}\n"
            f"Examples: {finding['examples']}"
        ),
        "labels": ["runtime", "env-sync", "friction"],
        "drift_signature": finding["drift_signature"],
    }
