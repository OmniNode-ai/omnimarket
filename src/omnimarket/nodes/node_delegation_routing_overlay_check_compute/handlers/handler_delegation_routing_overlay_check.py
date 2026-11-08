# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Refuse a routing overlay that leaves a task class without a frozen bar (OMN-22976).

Rebuild piece 15 keeps a routing overlay of per-task-class bars. Each class in the
task-class contract owns a bar (the score its answers must reach, the bounds a
request may move it within, the price it may cost and the time it may take). The
overlay freezes that bar under a full sha256 hash. For every class this check
refuses:

- a class with no bar in the overlay (``bar_missing``);
- a bar that does not parse, or a hash that is not a full sha256 (``bar_malformed``,
  ``hash_malformed``);
- a bar edited without a new hash (``hash_mismatch``);
- a contract bar that moved away from the frozen one (``bar_drift``), which is how a
  changed bar is told from a refrozen one: both the bar and the hash must be rewritten;
- a contract class that declares no bar of its own (``contract_bar_missing``);
- an overlay bar for a class the contract does not declare (``unknown_class``).

The handler is pure: both documents arrive parsed and nothing is read or written here.
``answer_command`` and ``events_of`` wrap it as the node's event chain: one command
in, one terminal event out, a failed check ending on the failed topic and a
NOT WORKING verdict.
"""

from __future__ import annotations

import json
import re

from pydantic import ValidationError

from omnimarket.nodes.node_delegation_routing_overlay_check_compute.models.model_routing_overlay_check import (
    EnumOverlayFindingClass,
    ModelRoutingOverlayCheckRequest,
    ModelRoutingOverlayCheckResult,
    ModelRoutingOverlayFinding,
)
from omnimarket.nodes.node_delegation_routing_overlay_check_compute.models.model_task_class_bar import (
    ModelTaskClassBar,
)

OVERLAY_SUBJECT = "<overlay>"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _mapping(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _contract_bar(entry: dict[str, object]) -> ModelTaskClassBar | None:
    gate = _mapping(entry.get("quality_gate"))
    try:
        return ModelTaskClassBar.model_validate(
            {
                "required_bar": gate.get("required_bar"),
                "request_override_bounds": gate.get("request_override_bounds"),
                "pricing_ceiling_per_1k_tokens": entry.get(
                    "pricing_ceiling_per_1k_tokens"
                ),
                "latency_sla_p99_ms": entry.get("latency_sla_p99_ms"),
            }
        )
    except ValidationError:
        return None


class HandlerDelegationRoutingOverlayCheck:
    """Judge the routing overlay against the task-class contract."""

    def handle(
        self, request: ModelRoutingOverlayCheckRequest
    ) -> ModelRoutingOverlayCheckResult:
        findings: list[ModelRoutingOverlayFinding] = []
        raw_classes = _mapping(request.task_class_contracts.get("task_classes"))
        raw_bars = _mapping(request.bars_overlay.get("task_class_bars"))
        classes = {k: v for k, v in raw_classes.items() if isinstance(k, str)}
        bars = {k: v for k, v in raw_bars.items() if isinstance(k, str)}
        for source, raw in (("contract", raw_classes), ("overlay", raw_bars)):
            findings.extend(
                self._finding(
                    OVERLAY_SUBJECT,
                    EnumOverlayFindingClass.OVERLAY_INVALID,
                    f"the {source} holds a task class key that is not a string: {key!r}",
                )
                for key in raw
                if not isinstance(key, str)
            )
        if not raw_bars:
            findings.append(
                self._finding(
                    OVERLAY_SUBJECT,
                    EnumOverlayFindingClass.OVERLAY_INVALID,
                    "the overlay holds no task_class_bars mapping",
                )
            )

        for name, raw_entry in classes.items():
            self._check_class(name, _mapping(raw_entry), bars.get(name), findings)

        findings.extend(
            self._finding(
                name,
                EnumOverlayFindingClass.UNKNOWN_CLASS,
                "the overlay holds a bar for a class the contract does not declare",
            )
            for name in bars
            if name not in classes
        )
        return ModelRoutingOverlayCheckResult(
            ok=not findings,
            checked_classes=len(classes),
            findings=tuple(findings),
        )

    @staticmethod
    def _finding(
        task_class: str, finding_class: EnumOverlayFindingClass, detail: str
    ) -> ModelRoutingOverlayFinding:
        return ModelRoutingOverlayFinding(
            task_class=task_class, finding_class=finding_class, detail=detail
        )

    def _check_class(
        self,
        name: str,
        entry: dict[str, object],
        raw_bar: object,
        findings: list[ModelRoutingOverlayFinding],
    ) -> None:
        if raw_bar is None:
            findings.append(
                self._finding(
                    name,
                    EnumOverlayFindingClass.BAR_MISSING,
                    "the class has no bar in the overlay",
                )
            )
            return
        slot = _mapping(raw_bar)
        try:
            frozen = ModelTaskClassBar.model_validate(slot.get("bar"))
        except ValidationError as exc:
            findings.append(
                self._finding(
                    name,
                    EnumOverlayFindingClass.BAR_MALFORMED,
                    f"the bar is missing or invalid: {exc.error_count()} error(s)",
                )
            )
            return

        declared = slot.get("bar_hash")
        if not isinstance(declared, str) or not _SHA256.match(declared):
            findings.append(
                self._finding(
                    name,
                    EnumOverlayFindingClass.HASH_MALFORMED,
                    "bar_hash is missing or is not a full lowercase sha256",
                )
            )
        elif declared != frozen.frozen_hash(name):
            findings.append(
                self._finding(
                    name,
                    EnumOverlayFindingClass.HASH_MISMATCH,
                    "the bar does not hash to its bar_hash; a changed bar needs a new hash",
                )
            )

        live = _contract_bar(entry)
        if live is None:
            findings.append(
                self._finding(
                    name,
                    EnumOverlayFindingClass.CONTRACT_BAR_MISSING,
                    "the contract class declares no complete bar to freeze",
                )
            )
        elif live != frozen:
            findings.append(
                self._finding(
                    name,
                    EnumOverlayFindingClass.BAR_DRIFT,
                    "the contract bar differs from the frozen bar; refreeze the bar and its hash together",
                )
            )


def events_of(
    result: ModelRoutingOverlayCheckResult, correlation_id: str, topics: dict[str, str]
) -> list[tuple[str, dict[str, object]]]:
    """The events a check produces: one terminal event under the request's correlation id."""
    body: dict[str, object] = {
        "correlation_id": correlation_id,
        "checked_classes": result.checked_classes,
        "verdict": "WORKING" if result.ok else "NOT WORKING",
        "findings": [
            {
                "task_class": f.task_class,
                "finding_class": f.finding_class.value,
                "detail": f.detail,
            }
            for f in result.findings
        ],
    }
    topic = topics["overlay_checked"] if result.ok else topics["overlay_check_failed"]
    return [(topic, body)]


def answer_command(
    payload: bytes,
    handler: HandlerDelegationRoutingOverlayCheck,
    topics: dict[str, str],
) -> list[tuple[str, bytes]]:
    """One command message in, the events it produces out, each as (topic, JSON bytes).

    A payload that is not a command is answered with a typed failure event, never dropped.
    """
    try:
        request = ModelRoutingOverlayCheckRequest.model_validate_json(payload)
    except ValidationError as exc:
        fields = sorted(
            {".".join(str(p) for p in e["loc"]) or "<document>" for e in exc.errors()}
        )
        failure = {
            "correlation_id": "unknown",
            "verdict": "NOT WORKING",
            "failure": f"bad-payload:{fields}",
        }
        return [(topics["overlay_check_failed"], json.dumps(failure).encode())]
    return [
        (topic, json.dumps(body, sort_keys=True).encode())
        for topic, body in events_of(
            handler.handle(request), request.correlation_id, topics
        )
    ]
