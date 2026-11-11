# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-21607: a delegation whose answer fails acceptance shows failed.

Standup 2026-10-30: delegations that failed on quality were shown as succeeding
and passing. The result carried ``status="completed"`` beside
``quality_gate_passed=false`` and the criteria it failed, and nothing treated
that as a failure: ``delegate_skill_succeeded`` answered True, the terminal was
routed as completed, and the row carried no cause. These tests hold the result
and its projection to one reading: the answer failed acceptance, so the run is
failed, with the reason on the row.

The golden chain is a passing terminal that stays a success; the error chain is
the same terminal with its acceptance failed. The injected break is the
``test_*_control`` pair: the same payload without the failed-acceptance record
must still project as it did before, so the new clause cannot be doing the work
for every ``quality_gate_passed=false`` terminal.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest
from omnibase_core.enums.enum_delegation_terminal_failure_cause import (
    EnumDelegationTerminalFailureCause,
)
from omnibase_core.enums.enum_quality_score_comparison import (
    EnumQualityScoreComparison,
)

from omnimarket.models.delegation.wire.model_delegate_skill_response import (
    ModelDelegateSkillCompleted,
    ModelDelegateSkillFailed,
    ModelDelegateSkillResponse,
    declares_failed_acceptance,
    delegate_skill_succeeded,
    delegate_skill_terminal_from_response,
)
from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
    TABLE,
    HandlerProjectionDelegation,
)
from omnimarket.nodes.node_projection_delegation.models.model_attempt_reduction import (
    reduce_delegation_attempts,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter
from tests.helpers.tenant_registry import (
    PROJECTION_TENANT_SLUG,
    seed_tenant_registry,
)

pytestmark = pytest.mark.unit

_CRITERION = "the answer must name the file it changed"
_CORRELATION = "7d1b0a52-3c4e-4f6a-9b8d-21607c0de001"

_FAILED_ACCEPTANCE_VARIANTS: dict[str, dict[str, Any]] = {
    "criterion": {"failed_acceptance_criteria": (_CRITERION,)},
    "gate": {"quality_gates_failed": ["names_the_file"]},
    "below_bar": {
        "quality_score": 0.4,
        "required_quality_bar": 0.8,
        "score_vs_required_bar": EnumQualityScoreComparison.BELOW_BAR,
    },
}


def _response(**overrides: Any) -> ModelDelegateSkillResponse:
    fields: dict[str, Any] = {
        "correlation_id": UUID(_CORRELATION),
        "status": "completed",
        "task_type": "review",
        "quality_gate_passed": False,
    }
    fields.update(overrides)
    return ModelDelegateSkillResponse(**fields)


@pytest.mark.parametrize("variant", sorted(_FAILED_ACCEPTANCE_VARIANTS))
def test_a_completed_answer_that_failed_acceptance_is_not_a_success(
    variant: str,
) -> None:
    response = _response(**_FAILED_ACCEPTANCE_VARIANTS[variant])

    assert declares_failed_acceptance(response) is True
    assert delegate_skill_succeeded(response) is False


@pytest.mark.parametrize("variant", sorted(_FAILED_ACCEPTANCE_VARIANTS))
def test_the_terminal_for_a_failed_acceptance_is_failed_and_names_why(
    variant: str,
) -> None:
    response = _response(**_FAILED_ACCEPTANCE_VARIANTS[variant])

    terminal = delegate_skill_terminal_from_response(response)

    assert isinstance(terminal, ModelDelegateSkillFailed)
    assert terminal.status == "failed"
    assert terminal.quality_gate_passed is False
    assert (
        terminal.terminal_failure_cause
        is EnumDelegationTerminalFailureCause.QUALITY_GATE_REFUSED
    )
    assert "acceptance" in terminal.error_message
    if variant == "criterion":
        assert _CRITERION in terminal.error_message


def test_control_a_passing_completed_answer_stays_completed() -> None:
    response = _response(quality_gate_passed=True)

    assert declares_failed_acceptance(response) is False
    assert delegate_skill_succeeded(response) is True
    assert isinstance(
        delegate_skill_terminal_from_response(response), ModelDelegateSkillCompleted
    )


def test_control_a_bare_false_flag_with_no_acceptance_record_is_unchanged() -> None:
    # Several ports never report the flag; absence of a record is not a failure.
    response = _response()

    assert declares_failed_acceptance(response) is False
    assert delegate_skill_succeeded(response) is True


def test_the_reducer_projects_a_declared_failed_acceptance_as_failed() -> None:
    result = reduce_delegation_attempts(
        declared_status="completed",
        declared_quality_gate_passed=False,
        attempts=(),
        declared_failed_acceptance=True,
    )

    assert result.terminal_ok is False
    assert (
        result.terminal_failure_cause
        is EnumDelegationTerminalFailureCause.QUALITY_GATE_REFUSED
    )


def test_control_the_reducer_without_the_record_keeps_its_old_reading() -> None:
    result = reduce_delegation_attempts(
        declared_status="completed",
        declared_quality_gate_passed=False,
        attempts=(),
    )

    assert result.terminal_ok is False
    assert result.terminal_failure_cause is None


def _project(event_type: str, **fields: Any) -> dict[str, Any]:
    db = InmemoryDatabaseAdapter()
    seed_tenant_registry(db)
    payload: dict[str, Any] = {
        "tenant_id": PROJECTION_TENANT_SLUG,
        "_db": db,
        "_event_type": event_type,
        "correlation_id": _CORRELATION,
        "task_type": "review",
    }
    payload.update(fields)
    result = HandlerProjectionDelegation().handle(payload)
    assert result["rows_upserted"] == 1
    rows = db.query(TABLE, {"correlation_id": _CORRELATION})
    assert len(rows) == 1
    return rows[0]


def test_golden_a_passing_terminal_projects_as_passing() -> None:
    row = _project(
        "delegate-skill-completed",
        status="completed",
        quality_gate_passed=True,
        response="done",
    )

    assert row["terminal_ok"] is True
    assert row["quality_gate_passed"] is True
    assert row["terminal_failure_cause"] is None


@pytest.mark.parametrize(
    "event_type", ["delegate-skill-completed", "delegate-skill-failed"]
)
def test_error_a_completed_status_with_failed_acceptance_projects_as_failed(
    event_type: str,
) -> None:
    row = _project(
        event_type,
        status="completed",
        quality_gate_passed=False,
        failed_acceptance_criteria=[_CRITERION],
        response="an answer that missed the criterion",
    )

    assert row["terminal_ok"] is False
    assert row["quality_gate_passed"] is False
    assert row["terminal_failure_cause"] == "quality_gate_refused"
    assert _CRITERION in row["quality_gate_detail"]


def test_control_the_same_terminal_without_the_record_keeps_no_cause() -> None:
    row = _project(
        "delegate-skill-completed",
        status="completed",
        quality_gate_passed=False,
        response="an answer",
    )

    assert row["terminal_failure_cause"] is None
