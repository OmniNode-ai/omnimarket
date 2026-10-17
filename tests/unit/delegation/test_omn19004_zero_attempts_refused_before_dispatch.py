# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19004 — a run that made no attempt says zero, and only that run may.

``attempts_count`` was floored at one, so a request refused before dispatch
(nothing was attempted, the attempt list is empty) published
``attempts_count=1``: a summary field asserting an attempt the record does not
contain. The field now holds zero, with the same two-sided discipline the rest
of this ticket uses:

* the producer that refused before dispatch states zero;
* the model refuses a zero that its own record contradicts, or that names any
  cause other than the refusal before dispatch.

A terminal that omits the count keeps the legacy floor of one; omnibase_core's
``ModelDelegationResult`` documents the same convention.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from omnibase_core.enums.enum_delegation_terminal_failure_cause import (
    EnumDelegationTerminalFailureCause,
)
from pydantic import ValidationError

from omnimarket.models.delegation.wire.model_delegate_skill_request import (
    ModelDelegateSkillRequest,
)
from omnimarket.models.delegation.wire.model_delegate_skill_response import (
    ModelDelegateSkillFailed,
    ModelDelegateSkillResponse,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.handlers import (
    handler_delegate_skill,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.handler_delegate_skill import (
    HandlerDelegateSkill,
)

pytestmark = pytest.mark.unit

_REFUSED = EnumDelegationTerminalFailureCause.REFUSED_BEFORE_DISPATCH


def _attempt() -> dict[str, Any]:
    return {
        "tier": "local",
        "backend_id": "b",
        "model_id": "m",
        "quality_gate_passed": False,
        "quality_score": 0.0,
        "cost_usd": 0.0,
    }


def _terminal(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "correlation_id": str(uuid4()),
        "status": "failed",
        "task_type": "document",
        "quality_gate_passed": False,
        "quality_score": 0.0,
    }
    payload.update(overrides)
    return payload


class _NeverDispatched:
    async def dispatch(self, **_kwargs: Any) -> dict[str, object]:
        raise AssertionError("a refused request must not reach the dispatch port")


@pytest.mark.asyncio
async def test_a_refusal_before_dispatch_states_zero_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RED: the 300s-against-240s refusal published attempts_count=1."""
    monkeypatch.setattr(
        handler_delegate_skill,
        "resolve_task_class_execution_budget",
        lambda _task_type: SimpleNamespace(
            task_class_timeout_ceiling_seconds=240,
            terminal_delivery_margin_seconds=60,
        ),
    )
    handler = HandlerDelegateSkill(dispatch_port=_NeverDispatched())

    response = await handler.handle(
        ModelDelegateSkillRequest(
            prompt="write a test",
            task_type="code_generation",
            source="claude-code",
            correlation_id=uuid4(),
            requested_timeout_seconds=300,
        )
    )

    assert isinstance(response, ModelDelegateSkillFailed)
    assert response.attempts == []
    assert response.attempts_count == 0
    assert response.terminal_failure_cause is _REFUSED


def test_zero_attempts_with_the_refusal_cause_constructs() -> None:
    model = ModelDelegateSkillResponse.model_validate(
        _terminal(attempts_count=0, terminal_failure_cause=_REFUSED)
    )

    assert model.attempts_count == 0
    assert model.attempts == []


def test_zero_attempts_beside_a_recorded_attempt_is_refused() -> None:
    """A count of zero cannot sit beside a record of an attempt."""
    with pytest.raises(ValidationError) as excinfo:
        ModelDelegateSkillResponse.model_validate(
            _terminal(
                attempts_count=0,
                attempts=[_attempt()],
                terminal_failure_cause=_REFUSED,
            )
        )

    message = str(excinfo.value)
    assert "attempts_count (0)" in message
    assert "1 attempt record" in message


def test_zero_attempts_without_the_refusal_cause_is_refused() -> None:
    """Only a request refused before dispatch made no attempt."""
    with pytest.raises(ValidationError) as excinfo:
        ModelDelegateSkillResponse.model_validate(
            _terminal(
                attempts_count=0,
                terminal_failure_cause=EnumDelegationTerminalFailureCause.TIMEOUT,
            )
        )

    assert "refused_before_dispatch" in str(excinfo.value)


def test_zero_attempts_with_no_cause_is_refused() -> None:
    with pytest.raises(ValidationError) as excinfo:
        ModelDelegateSkillResponse.model_validate(_terminal(attempts_count=0))

    assert "refused_before_dispatch" in str(excinfo.value)


def test_a_negative_count_is_still_refused() -> None:
    """Positive control: the widening stops at zero."""
    with pytest.raises(ValidationError):
        ModelDelegateSkillResponse.model_validate(_terminal(attempts_count=-1))


def test_an_omitted_count_with_no_record_keeps_the_legacy_floor_of_one() -> None:
    """Positive control: omitting the count never derives zero."""
    model = ModelDelegateSkillResponse.model_validate(
        _terminal(status="timeout", attempts=[])
    )

    assert model.attempts_count == 1
