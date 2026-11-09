# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Judge one synthetic delegated task from its terminal outcome (OMN-22977, rebuild piece 15).

``handle(ModelSyntheticProbeRequest) -> ModelSyntheticProbeResult``. A pure function: it reads
no clock, network or environment. The task's prompt, expected answer, bound, cadence and the
row's lane, source and piece are the contract's ``probe_rules`` and nowhere else.

A task passes when a terminal came back inside the bound with a result holding the expected
answer and a cost. Each way to fail has its own reason and still produces the ledger row the
reading reads, so a dropped result is a FAIL row, never silence.
"""

from __future__ import annotations

import math
from pathlib import Path

import yaml

from omnimarket.nodes.node_delegation_synthetic_probe_compute.models.model_delegation_synthetic_probe import (
    EnumSyntheticProbeReason,
    EnumSyntheticProbeVerdict,
    ModelSyntheticProbeRequest,
    ModelSyntheticProbeResult,
    ModelSyntheticProbeRules,
)

_CONTRACT = Path(__file__).parents[1] / "contract.yaml"


def load_rules(contract: Path = _CONTRACT) -> ModelSyntheticProbeRules:
    """Read the ``probe_rules`` of a contract; a missing or unknown rule is refused."""
    data = yaml.safe_load(contract.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("probe_rules"), dict):
        raise ValueError("contract probe_rules must be a mapping")
    return ModelSyntheticProbeRules.model_validate(data["probe_rules"])


def _token(value: str) -> str:
    """A value as one ledger-cell-safe token."""
    return "".join(c if c.isalnum() or c in "._:-" else "-" for c in value) or "-"


def _finite(value: float | None) -> float | None:
    """A cost as a usable amount: a non-finite value is no cost at all."""
    return value if value is not None and math.isfinite(value) else None


class HandlerDelegationSyntheticProbe:
    """Pure compute over one task outcome and the contract's probe_rules."""

    def __init__(self, rules: ModelSyntheticProbeRules | None = None) -> None:
        self._rules = rules if rules is not None else load_rules()

    @property
    def rules(self) -> ModelSyntheticProbeRules:
        return self._rules

    def _reason(
        self, request: ModelSyntheticProbeRequest, elapsed: float | None
    ) -> EnumSyntheticProbeReason:
        rules = self._rules
        cost = _finite(request.cost_usd)
        if request.observed_at is None or request.terminal_event_type is None:
            return EnumSyntheticProbeReason.NO_TERMINAL
        if request.terminal_event_type != rules.success_terminal_event_type:
            return EnumSyntheticProbeReason.NOT_SUCCESS_TERMINAL
        if elapsed is not None and elapsed > rules.bound_seconds:
            return EnumSyntheticProbeReason.OVER_BOUND
        if not (request.result_text or "").strip():
            return EnumSyntheticProbeReason.RESULT_DROPPED
        if rules.expected_substring.lower() not in (request.result_text or "").lower():
            return EnumSyntheticProbeReason.WRONG_ANSWER
        if rules.require_cost and (cost is None or cost < 0):
            return EnumSyntheticProbeReason.COST_MISSING
        return EnumSyntheticProbeReason.OK

    def handle(self, request: ModelSyntheticProbeRequest) -> ModelSyntheticProbeResult:
        rules = self._rules
        cost = _finite(request.cost_usd)
        exact = (
            None
            if request.observed_at is None
            else (request.observed_at - request.issued_at).total_seconds()
        )
        elapsed = None if exact is None else int(exact)
        reason = self._reason(request, exact)
        verdict = (
            EnumSyntheticProbeVerdict.PASS
            if reason is EnumSyntheticProbeReason.OK
            else EnumSyntheticProbeVerdict.FAIL
        )
        cells = {
            "src": rules.row_source,
            "piece": str(rules.row_piece),
            "task": _token(request.task_id),
            "task_class": _token(rules.task_class),
            "verdict": verdict.value,
            "reason": reason.value,
            "result": ("present" if (request.result_text or "").strip() else "dropped"),
            "cost_usd": "none" if cost is None else f"{cost:.6f}",
            "elapsed_s": "none" if elapsed is None else str(elapsed),
            "bound_s": str(rules.bound_seconds),
        }
        text = " | ".join(f"{name}={value}" for name, value in cells.items())
        return ModelSyntheticProbeResult(
            correlation_id=request.correlation_id,
            task_id=request.task_id,
            verdict=verdict,
            reason=reason,
            failure_reason=(
                None if verdict is EnumSyntheticProbeVerdict.PASS else reason.value
            ),
            elapsed_seconds=elapsed,
            cost_usd=cost,
            row_cells=cells,
            row_text=f"lane={rules.row_lane} | {text}",
            row_stamp=request.observed_at or request.issued_at,
        )
