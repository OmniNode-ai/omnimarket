# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19437 AC1, consumer half: request and terminal decode trace_id and span_id.

The delegation command and terminal will carry ``trace_id`` and ``span_id`` once
the producers (the CLI, then the handler) stamp them. The Wire Compatibility
Gate (OMN-18868) wants a consumer able to decode a new key to be RELEASED before
any producer emits it, so this release decodes both keys on the delegate-skill
request and on the terminal and drops them, as the other forthcoming keys are.
The field declarations and the handler's copy are the second half and are not
in this change. Every other unknown key is still refused with ``extra_forbidden``.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from omnimarket.models.delegation.wire.model_delegate_skill_request import (
    ModelDelegateSkillRequest,
)
from omnimarket.models.delegation.wire.model_delegate_skill_response import (
    ModelDelegateSkillCompleted,
    ModelDelegateSkillFailed,
    ModelDelegateSkillResponse,
)

pytestmark = pytest.mark.unit

_TRACE_KEYS = ("trace_id", "span_id")


def _request_payload() -> dict[str, object]:
    return {
        "prompt": "summarise the log",
        "task_type": "document",
        "source": "claude-code",
        "correlation_id": str(uuid4()),
    }


def _terminal_payload() -> dict[str, object]:
    return {
        "correlation_id": str(uuid4()),
        "status": "completed",
        "task_type": "document",
    }


@pytest.mark.parametrize("key", _TRACE_KEYS)
def test_the_request_decodes_a_trace_or_span_id(key: str) -> None:
    """A command carrying trace_id or span_id decodes instead of extra_forbidden."""
    payload = {**_request_payload(), key: str(uuid4())}
    request = ModelDelegateSkillRequest.model_validate(payload)
    assert request.prompt == "summarise the log"
    assert key not in request.model_dump()


def test_the_request_decodes_both_ids_together() -> None:
    """The producer sends the pair; the consumer decodes the pair."""
    payload = {
        **_request_payload(),
        "trace_id": str(uuid4()),
        "span_id": str(uuid4()),
    }
    request = ModelDelegateSkillRequest.model_validate(payload)
    assert not set(_TRACE_KEYS) & set(request.model_dump())


@pytest.mark.parametrize("key", _TRACE_KEYS)
@pytest.mark.parametrize(
    "model", [ModelDelegateSkillResponse, ModelDelegateSkillFailed]
)
def test_the_terminal_decodes_a_trace_or_span_id(
    model: type[ModelDelegateSkillResponse], key: str
) -> None:
    """A terminal carrying trace_id or span_id decodes instead of extra_forbidden."""
    payload = {**_terminal_payload(), key: str(uuid4())}
    if model is ModelDelegateSkillFailed:
        payload["status"] = "failed"
    terminal = model.model_validate(payload)
    assert key not in terminal.model_dump()


def test_the_completed_terminal_decodes_both_ids() -> None:
    """The completed topic variant inherits the tolerance."""
    payload = {
        **_terminal_payload(),
        "quality_gate_passed": True,
        "quality_score": 1.0,
        "trace_id": str(uuid4()),
        "span_id": str(uuid4()),
    }
    terminal = ModelDelegateSkillCompleted.model_validate(payload)
    assert not set(_TRACE_KEYS) & set(terminal.model_dump())


def test_legacy_payloads_without_the_ids_still_decode() -> None:
    """Control: a command and terminal emitted before the ids still decode."""
    assert ModelDelegateSkillRequest.model_validate(_request_payload())
    assert ModelDelegateSkillResponse.model_validate(_terminal_payload())


def test_a_misspelt_key_is_still_refused_on_both_models() -> None:
    """Control: tolerating the two ids must not relax extra=forbid for others."""
    cases = (
        (ModelDelegateSkillRequest, _request_payload()),
        (ModelDelegateSkillResponse, _terminal_payload()),
    )
    for model, base in cases:
        payload = {**base, "trace_id": str(uuid4()), "trace_idd": str(uuid4())}
        with pytest.raises(ValidationError) as excinfo:
            model.model_validate(payload)
        assert any(
            error["loc"] == ("trace_idd",) and error["type"] == "extra_forbidden"
            for error in excinfo.value.errors()
        )
