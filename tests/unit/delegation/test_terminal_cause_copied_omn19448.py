# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19448 AC1: the projection copies the terminal's own failure cause.

Before this change the ``delegation_events`` row never read the terminal's
``terminal_failure_cause``. The skill-terminal path re-derived a cause from the
attempt ladder, which only knows one member (``provider_quota_exhausted``); the
canonical ``delegation-completed/failed`` path named no cause at all. So probe
run ``1aeccaa6`` said ``provider_error`` on the wire and NULL on the row, and
93 of 144 failed rows in seven days carried a NULL cause.

The fold tests below were drafted by ``onex delegate`` (run
``4a3b43b6-f0b7-4cf4-bae1-5486466ee3e5``, Qwen3.8-27B) from the stated
behaviour and edited for typing and for the three required attempt-record
fields the prompt did not state.
"""

from __future__ import annotations

from typing import Any

import pytest
from omnibase_core.enums.enum_delegation_terminal_failure_cause import (
    EnumDelegationTerminalFailureCause,
)

from omnimarket.models.delegation.wire.model_delegate_skill_response import (
    ModelDelegateSkillAttemptRecord,
)
from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
    ModelProjectionTaskDelegatedEvent,
    _canonical_result_to_task_delegated_payload,
)
from omnimarket.nodes.node_projection_delegation.models.model_attempt_reduction import (
    reduce_delegation_attempts,
)

_RATE_LIMITED = (
    ModelDelegateSkillAttemptRecord(
        tier="local",
        backend_id="qwen3-local",
        model_id="Qwen3.8-27B",
        quality_gate_passed=False,
        failure_class="rate_limited",
    ),
)


@pytest.mark.unit
def test_quality_gate_refused_with_empty_attempts() -> None:
    result = reduce_delegation_attempts(
        declared_status="failed",
        declared_quality_gate_passed=False,
        error_message="",
        attempts=(),
        declared_failure_cause=EnumDelegationTerminalFailureCause.QUALITY_GATE_REFUSED,
    )
    assert (
        result.terminal_failure_cause
        == EnumDelegationTerminalFailureCause.QUALITY_GATE_REFUSED
    )
    assert result.terminal_ok is False


@pytest.mark.unit
def test_provider_error_overrides_rate_limited_ladder() -> None:
    result = reduce_delegation_attempts(
        declared_status="failed",
        declared_quality_gate_passed=False,
        error_message="",
        attempts=_RATE_LIMITED,
        declared_failure_cause=EnumDelegationTerminalFailureCause.PROVIDER_ERROR,
    )
    assert (
        result.terminal_failure_cause
        == EnumDelegationTerminalFailureCause.PROVIDER_ERROR
    )
    assert result.terminal_ok is False
    assert result.attempt_history == _RATE_LIMITED, "the ladder is still persisted"


@pytest.mark.unit
def test_rate_limited_ladder_fallback_to_provider_quota_exhausted() -> None:
    # Positive control: a terminal that names no cause keeps the ladder fallback.
    result = reduce_delegation_attempts(
        declared_status="failed",
        declared_quality_gate_passed=False,
        error_message="",
        attempts=_RATE_LIMITED,
        declared_failure_cause=None,
    )
    assert (
        result.terminal_failure_cause
        == EnumDelegationTerminalFailureCause.PROVIDER_QUOTA_EXHAUSTED
    )
    assert result.terminal_ok is False


@pytest.mark.unit
def test_completed_with_quality_gate_passed_yields_terminal_ok() -> None:
    result = reduce_delegation_attempts(
        declared_status="completed",
        declared_quality_gate_passed=True,
        error_message="",
        attempts=(),
        declared_failure_cause=None,
    )
    assert result.terminal_ok is True
    assert result.terminal_failure_cause is None


@pytest.mark.unit
@pytest.mark.parametrize("failure_cause", list(EnumDelegationTerminalFailureCause))
def test_parametrized_declared_failure_cause_passthrough(
    failure_cause: EnumDelegationTerminalFailureCause,
) -> None:
    result = reduce_delegation_attempts(
        declared_status="failed",
        declared_quality_gate_passed=False,
        error_message="",
        attempts=(),
        declared_failure_cause=failure_cause,
    )
    assert result.terminal_failure_cause == failure_cause
    assert result.terminal_ok is False


def _canonical_failed_payload(cause: str | None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "correlation_id": "0b6f1d2e-9a57-4c1e-8f0e-3e1f9c0a4d11",
        "task_type": "test",
        "model_used": "qwen3-coder-30b",
        "quality_passed": False,
        "failure_reason": "HTTP 429: rate limit exceeded",
        "operational_outcome": "provider_quota",
        "content_verdict": "not_applicable",
    }
    if cause is not None:
        payload["terminal_failure_cause"] = cause
    return payload


@pytest.mark.unit
@pytest.mark.parametrize("failure_cause", list(EnumDelegationTerminalFailureCause))
def test_the_canonical_converter_carries_the_terminal_cause(
    failure_cause: EnumDelegationTerminalFailureCause,
) -> None:
    normalized = _canonical_result_to_task_delegated_payload(
        _canonical_failed_payload(failure_cause.value)
    )
    event = ModelProjectionTaskDelegatedEvent(**normalized)
    assert event.terminal_failure_cause == failure_cause


@pytest.mark.unit
def test_a_canonical_terminal_without_a_cause_converts_to_none() -> None:
    # A terminal produced before the cause existed still converts, with no cause.
    event = ModelProjectionTaskDelegatedEvent(
        **_canonical_result_to_task_delegated_payload(_canonical_failed_payload(None))
    )
    assert event.terminal_failure_cause is None
