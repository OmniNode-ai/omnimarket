# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19556: validate and preserve the answered draft's source on the wire."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from omnibase_core.models.delegation.wire import ModelDelegationBudgetEvidence
from pydantic import ValidationError

from omnimarket.models.delegation.wire.model_delegate_skill_request import (
    ModelDelegateSkillRequest,
)
from omnimarket.models.delegation.wire.model_delegate_skill_response import (
    ModelDelegateSkillFailed,
    ModelDelegateSkillResponse,
    delegate_skill_terminal_from_response,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.handler_delegate_skill import (
    _response_from_result,
)

pytestmark = pytest.mark.unit

_ANSWER = "def test_registration():\n    assert register('node') is True"
_SOURCE = {"attempt_index": 1, "tier": "local", "backend_id": "local-backend"}


def _payload() -> dict[str, Any]:
    """A nonzero source index distinguishes a history index from a tier index."""
    return {
        "correlation_id": str(uuid4()),
        "status": "failed",
        "task_type": "test",
        "response": _ANSWER,
        "quality_gate_passed": False,
        "quality_score": 0.0,
        "terminal_failure_cause": "quality_gate_refused",
        "attempts_count": 3,
        "attempts": [
            {
                "tier": "local",
                "backend_id": "local-backend",
                "model_id": "local-model",
                "quality_gate_passed": False,
                "quality_score": score,
                "failure_class": None,
                "acceptance_decision": "climb",
                "acceptance_reason": "deterministic_floor_failed",
            }
            for score in (0.4, 0.567)
        ]
        + [
            {
                "tier": "cheap_cloud",
                "backend_id": "glm-5.3-flash",
                "model_id": "glm-5.3-flash",
                "quality_gate_passed": False,
                "quality_score": 0.0,
                # The bus can record a provider failure WITHOUT failure_class.
                "failure_class": None,
                "acceptance_decision": "climb",
                "acceptance_reason": "provider_call_failed",
                "error_message": "The read operation timed out [ReadTimeout]",
            }
        ],
    }


def _with_source() -> dict[str, Any]:
    payload = _payload()
    # Prove the fixture is otherwise valid before exercising the new field.
    ModelDelegateSkillFailed.model_validate(payload)
    return {**payload, "response_source_attempt": dict(_SOURCE)}


@pytest.mark.parametrize(
    "model", [ModelDelegateSkillResponse, ModelDelegateSkillFailed]
)
def test_accepts_named_answered_attempt(
    model: type[ModelDelegateSkillResponse],
) -> None:
    response = model.model_validate(_with_source())
    source = response.response_source_attempt
    assert source is not None
    assert type(source).__name__ == "ModelDelegateSkillResponseSourceAttempt"
    assert source.model_dump(mode="json") == _SOURCE
    assert response.response == _ANSWER
    assert response.attempts[source.attempt_index].quality_gate_passed is False
    # Preserve the reference through JSON serialization, not only in memory.
    assert (
        model.model_validate_json(response.model_dump_json()).response_source_attempt
        == source
    )


@pytest.mark.parametrize(
    "case",
    [
        "out_of_range",
        "negative_index",
        "provider_call_failed",
        "failure_class",
        "tier_mismatch",
        "backend_mismatch",
        "empty_response",
    ],
)
def test_refuses_invalid_response_source_attempt(case: str) -> None:
    payload = _with_source()
    source = payload["response_source_attempt"]
    if case == "out_of_range":
        source["attempt_index"] = len(payload["attempts"])
    elif case == "negative_index":
        source["attempt_index"] = -1
    elif case == "provider_call_failed":
        source.update(attempt_index=2, tier="cheap_cloud", backend_id="glm-5.3-flash")
    elif case == "failure_class":
        payload["attempts"][1]["failure_class"] = "model_unavailable"
    elif case == "tier_mismatch":
        source["tier"] = "cheap_cloud"
    elif case == "backend_mismatch":
        source["backend_id"] = "wrong-backend"
    elif case == "empty_response":
        payload["response"] = ""

    # Rejecting an unknown top-level key is NOT validation of its meaning.
    ModelDelegateSkillFailed.model_validate(
        {
            key: value
            for key, value in payload.items()
            if key != "response_source_attempt"
        }
    )
    with pytest.raises(ValidationError) as excinfo:
        ModelDelegateSkillFailed.model_validate(payload)
    errors = excinfo.value.errors()
    assert not any(
        error["type"] == "extra_forbidden"
        and error["loc"] == ("response_source_attempt",)
        for error in errors
    ), (
        "response_source_attempt is still an unknown field; its invariant was not checked"
    )
    assert any(
        error["type"] in {"value_error", "greater_than_equal"} for error in errors
    )


def test_source_attempt_is_frozen() -> None:
    response = ModelDelegateSkillFailed.model_validate(_with_source())
    assert response.response_source_attempt is not None
    with pytest.raises(ValidationError) as excinfo:
        response.response_source_attempt.attempt_index = 0
    assert excinfo.value.errors()[0]["type"] == "frozen_instance"


def test_source_attempt_forbids_extra_fields() -> None:
    payload = _with_source()
    payload["response_source_attempt"]["unexpected"] = "not part of the source contract"
    with pytest.raises(ValidationError) as excinfo:
        ModelDelegateSkillFailed.model_validate(payload)
    assert any(
        error["type"] == "extra_forbidden"
        and error["loc"] == ("response_source_attempt", "unexpected")
        for error in excinfo.value.errors()
    ), (
        "the nested source model must reject extras, rather than reject the whole new field"
    )


def test_decodes_without_response_source_attempt() -> None:
    """Existing terminals remain readable, including older nonempty responses."""
    response = ModelDelegateSkillFailed.model_validate(_payload())
    assert response.response == _ANSWER
    assert response.model_dump(mode="json").get("response_source_attempt") is None


def test_decodes_explicit_null_response_source_attempt() -> None:
    response = ModelDelegateSkillFailed.model_validate(
        {**_payload(), "response_source_attempt": None}
    )
    assert response.response_source_attempt is None


def test_response_from_result_preserves_source_in_failed_terminal() -> None:
    """Flattened bus history uses backend_ref; the skill wire uses backend_id."""
    payload = _payload()
    request = ModelDelegateSkillRequest(
        prompt="Write registration tests", task_type="test", source="external-client"
    )
    result: dict[str, object] = {
        "status": "failed",
        "content": _ANSWER,
        "quality_passed": False,
        "quality_score": None,
        "failure_reason": "The read operation timed out [ReadTimeout]",
        "attempts_count": 3,
        "escalation_count": 1,
        "response_source_attempt": dict(_SOURCE),
        "escalation_history": [
            {
                "tier_name": attempt["tier"],
                "backend_ref": attempt["backend_id"],
                "model_used": attempt["model_id"],
                "quality_score": attempt["quality_score"],
                "acceptance_decision": attempt["acceptance_decision"],
                "acceptance_reason": attempt["acceptance_reason"],
                "failure_reasons": [
                    attempt.get("error_message", "deterministic_floor_failed")
                ],
            }
            for attempt in payload["attempts"]
        ],
    }
    response = _response_from_result(
        request,
        result,
        tenant_id=None,
        queue_wait_ms=None,
        execution_duration_ms=30,
        budget_evidence=ModelDelegationBudgetEvidence(
            requested_timeout_seconds=None,
            task_class_timeout_ceiling_seconds=60,
            execution_timeout_seconds=60,
            terminal_delivery_margin_seconds=5,
        ),
    )
    terminal = delegate_skill_terminal_from_response(response)
    assert isinstance(terminal, ModelDelegateSkillFailed)
    assert terminal.response == _ANSWER
    assert len(terminal.attempts) == 3
    assert terminal.attempts[1].backend_id == _SOURCE["backend_id"]
    assert terminal.model_dump(mode="json").get("response_source_attempt") == _SOURCE, (
        "_response_from_result dropped the bus terminal's response_source_attempt"
    )
