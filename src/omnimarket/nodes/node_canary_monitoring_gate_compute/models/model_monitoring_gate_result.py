# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ModelMonitoringGateResult — overall canary monitoring-gate verdict (OMN-14735, B10).

The overall status is deliberately a **four**-state outcome, not the usual
three (PASS/WARN/ABORT): ``BLOCKED_PENDING_A6`` is a first-class result so a
caller cannot mistake "no numeric thresholds have landed yet" for "the gate
passed". The gate only ever reports PASS/WARN/ABORT once every gated signal
has a fully-resolved threshold spec.
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_canary_monitoring_gate_compute.models.model_signal_reading import (
    SignalName,
)
from omnimarket.nodes.node_canary_monitoring_gate_compute.models.model_signal_verdict import (
    ModelSignalVerdict,
)

OverallStatus = Literal["PASS", "WARN", "ABORT", "BLOCKED_PENDING_A6"]

# The default abort action per the B10 acceptance note in the managed-staging
# task split ("Define the abort action for a B10 threshold breach ... a
# threshold without an action is advisory."): stop the single canary
# producer, halt consumers, and hand off to the B13 teardown/rollback
# runbook. This is a *procedural* default, not a numeric threshold, so it
# does not require A6 and is safe to scaffold now.
DEFAULT_ABORT_ACTION = (
    "stop_canary_producer; halt_canary_consumers; "
    "invoke docs/runbooks/managed-staging-canary-teardown-rollback.md (B13)"
)


class ModelMonitoringGateResult(BaseModel):
    """Overall verdict of the canary monitoring gate for one evaluation window.

    Attributes:
        correlation_id: Echo of the input ``correlation_id``.
        verdicts: Per-signal classification (see
            :class:`~...model_signal_verdict.ModelSignalVerdict`).
        overall_status: The aggregate outcome (see module docstring).
        abort_action: The action to take when ``overall_status == "ABORT"``.
            ``None`` otherwise.
        unresolved_signals: Signal domains whose threshold spec is not yet
            fully resolved (A6 pending).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID | None = Field(
        default=None, description="Echo of the input correlation_id."
    )
    verdicts: tuple[ModelSignalVerdict, ...] = Field(
        default=(), description="Per-signal classification."
    )
    overall_status: OverallStatus = Field(..., description="Aggregate outcome.")
    abort_action: str | None = Field(
        default=None,
        description="Action to take when overall_status is ABORT; None otherwise.",
    )
    unresolved_signals: tuple[SignalName, ...] = Field(
        default=(),
        description="Signal domains without a fully-resolved threshold (A6 pending).",
    )


__all__ = ["DEFAULT_ABORT_ACTION", "ModelMonitoringGateResult", "OverallStatus"]
