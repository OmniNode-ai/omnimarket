# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19448 AC3: a failed row whose ladder the gate decided never lands a NULL cause.

AC3 reads "on the lab, no failed row written after the deploy has a null
cause". The .201 dev lane's ``delegation_events`` held failed rows with a NULL
``terminal_failure_cause`` whose ``attempt_history`` shows the quality gate
refusing every rung (``climb`` / ``heuristic_veto``, ``failure_class``
``quality_gate_failed``), for example correlation
``04895634-86a4-4f4f-8bea-079e76f36197`` (3 rungs, ``TASK_MISMATCH``). The
projection copies a terminal's own cause unchanged, so a terminal that names no
cause left the row NULL, even though the ladder it carried says what decided the
run. ``reduce_delegation_attempts`` derived a cause from the ladder only for a
capacity refusal.

The fix reads the same rule the producers apply
(``omnimarket.delegation.deciding_cause.ladder_is_gate_decided``), and only
when the terminal declared no cause: a declared cause still wins unchanged.
"""

from __future__ import annotations

import pytest
from omnibase_core.enums.enum_delegation_terminal_failure_cause import (
    EnumDelegationTerminalFailureCause,
)

from omnimarket.enums.enum_delegation_acceptance import (
    EnumDelegationAcceptanceDecision,
    EnumDelegationAcceptanceReason,
)
from omnimarket.models.delegation.wire.model_delegate_skill_response import (
    ModelDelegateSkillAttemptRecord,
)
from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
    TABLE,
    HandlerProjectionDelegation,
)
from omnimarket.nodes.node_projection_delegation.models.model_attempt_reduction import (
    ModelDelegationAttemptReduction,
    reduce_delegation_attempts,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter
from tests.helpers.tenant_registry import (
    PROJECTION_TENANT_SLUG,
    seed_tenant_registry,
)

pytestmark = pytest.mark.unit

_CORRELATION_ID = "04895634-86a4-4f4f-8bea-079e76f36197"


def _rung(
    *,
    decision: EnumDelegationAcceptanceDecision,
    reason: EnumDelegationAcceptanceReason,
    failure_class: str | None,
    passed: bool = False,
) -> ModelDelegateSkillAttemptRecord:
    return ModelDelegateSkillAttemptRecord(
        tier="local",
        backend_id="local-heavy-reasoning",
        model_id="Qwen3.8-27B",
        quality_gate_passed=passed,
        failure_class=failure_class,
        acceptance_decision=decision,
        acceptance_reason=reason,
    )


def _gate_refused_rung() -> ModelDelegateSkillAttemptRecord:
    return _rung(
        decision=EnumDelegationAcceptanceDecision.CLIMB,
        reason=EnumDelegationAcceptanceReason.HEURISTIC_VETO,
        failure_class="quality_gate_failed",
    )


def _rate_limited_rung() -> ModelDelegateSkillAttemptRecord:
    return _rung(
        decision=EnumDelegationAcceptanceDecision.CLIMB,
        reason=EnumDelegationAcceptanceReason.PROVIDER_CALL_FAILED,
        failure_class="rate_limited",
    )


def _reduce(
    attempts: tuple[ModelDelegateSkillAttemptRecord, ...],
    *,
    declared: EnumDelegationTerminalFailureCause | None = None,
    declared_status: str = "failed",
) -> ModelDelegationAttemptReduction:
    return reduce_delegation_attempts(
        declared_status=declared_status,
        declared_quality_gate_passed=False,
        error_message="",
        attempts=attempts,
        declared_failure_cause=declared,
    )


def test_a_ladder_the_gate_refused_names_the_gate_when_the_terminal_names_nothing() -> (
    None
):
    # The measured row: three rungs, every one answered and vetoed.
    result = _reduce((_gate_refused_rung(),) * 3)

    assert (
        result.terminal_failure_cause
        is EnumDelegationTerminalFailureCause.QUALITY_GATE_REFUSED
    )
    assert result.terminal_ok is False


def test_a_terminating_gate_refusal_names_the_gate() -> None:
    # A shape veto no costlier rung can satisfy ends the ladder: ``terminate``.
    terminated = _rung(
        decision=EnumDelegationAcceptanceDecision.TERMINATE,
        reason=EnumDelegationAcceptanceReason.HEURISTIC_VETO,
        failure_class="quality_gate_failed",
    )

    result = _reduce((terminated,))

    assert (
        result.terminal_failure_cause
        is EnumDelegationTerminalFailureCause.QUALITY_GATE_REFUSED
    )


def test_a_rate_limit_after_gate_refusals_does_not_decide_the_run() -> None:
    # OMN-19004: the gate decided; the 429 only stopped the ladder collecting
    # another answer. The ladder fallback must agree with the producers.
    result = _reduce((_gate_refused_rung(), _gate_refused_rung(), _rate_limited_rung()))

    assert (
        result.terminal_failure_cause
        is EnumDelegationTerminalFailureCause.QUALITY_GATE_REFUSED
    )


def test_a_declared_cause_still_wins_over_the_ladder() -> None:
    result = _reduce(
        (_gate_refused_rung(),),
        declared=EnumDelegationTerminalFailureCause.PROVIDER_ERROR,
    )

    assert (
        result.terminal_failure_cause
        is EnumDelegationTerminalFailureCause.PROVIDER_ERROR
    )


def test_a_ladder_with_no_gate_judgement_keeps_the_capacity_fallback() -> None:
    # Negative control: nothing was answered and judged, so the gate decided
    # nothing and the existing capacity reading is unchanged.
    result = _reduce((_rate_limited_rung(),))

    assert (
        result.terminal_failure_cause
        is EnumDelegationTerminalFailureCause.PROVIDER_QUOTA_EXHAUSTED
    )


def test_a_ladder_that_accepted_a_rung_carries_no_cause() -> None:
    # Negative control: an accepted rung ends the escalation in acceptance, so
    # the earlier refusals are history and a success may not carry a cause.
    accepted = _rung(
        decision=EnumDelegationAcceptanceDecision.ACCEPT,
        reason=EnumDelegationAcceptanceReason.QUALITY_BAR_MET,
        failure_class=None,
        passed=True,
    )

    result = reduce_delegation_attempts(
        declared_status="completed",
        declared_quality_gate_passed=True,
        error_message="",
        attempts=(_gate_refused_rung(), accepted),
        declared_failure_cause=None,
    )

    assert result.terminal_failure_cause is None
    assert result.terminal_ok is True


def test_a_failed_ladder_with_no_typed_decision_stays_unclassified() -> None:
    # Negative control: a rung that records no typed decision never reached the
    # gate, and that absence is not read as a refusal (deciding_cause rule).
    untyped = ModelDelegateSkillAttemptRecord(
        tier="local",
        backend_id="local-heavy-reasoning",
        model_id="Qwen3.8-27B",
        quality_gate_passed=False,
        failure_class="quality_gate_failed",
    )

    result = _reduce((untyped,))

    assert result.terminal_failure_cause is None
    assert result.terminal_ok is False


def _skill_failed_payload() -> dict[str, object]:
    """A ``delegate-skill-failed`` payload that names no cause but carries the ladder."""
    return {
        "tenant_id": PROJECTION_TENANT_SLUG,
        "_event_type": "delegate-skill-failed",
        "status": "failed",
        "correlation_id": _CORRELATION_ID,
        "task_type": "summarization",
        "model_name": "Qwen3.8-27B",
        "quality_gate_passed": False,
        "error_message": "TASK_MISMATCH: response explicitly disclaims accuracy",
        "attempts": [_gate_refused_rung().model_dump(mode="json") for _ in range(3)],
    }


def test_the_row_for_a_gate_refused_ladder_carries_the_cause() -> None:
    db = InmemoryDatabaseAdapter()
    seed_tenant_registry(db)

    result = HandlerProjectionDelegation().handle(
        {**_skill_failed_payload(), "_db": db}
    )

    assert result["rows_upserted"] == 1
    rows = db.query(TABLE, {"correlation_id": _CORRELATION_ID})
    assert len(rows) == 1
    assert rows[0]["terminal_ok"] is False
    assert rows[0]["terminal_failure_cause"] == "quality_gate_refused"
