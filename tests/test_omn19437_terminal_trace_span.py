# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19437 AC1, producer half: the terminal copies its command's trace and span ids.

The consumer half (the released decoder) made the delegate-skill request and
terminal accept ``trace_id`` and ``span_id``. This half declares them on both
models and has the handler copy the command's ids onto every terminal it builds,
so a ``delegation_events`` row can be joined to the trace that issued it.
"""

from __future__ import annotations

from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from omnimarket.models.delegation.wire.model_delegate_skill_request import (
    ModelDelegateSkillRequest,
)
from omnimarket.models.delegation.wire.model_delegate_skill_response import (
    ModelDelegateSkillFailed,
    ModelDelegateSkillResponse,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.handler_delegate_skill import (
    HandlerDelegateSkill,
    ProtocolDelegationDispatchPort,
)

pytestmark = pytest.mark.unit


class _RaisingDispatchPort:
    """A dispatch that fails, so the handler builds the failed terminal."""

    async def dispatch(self, **_kwargs: Any) -> dict[str, object]:
        raise RuntimeError("provider unreachable")


def _handler() -> HandlerDelegateSkill:
    return HandlerDelegateSkill(
        dispatch_port=cast(ProtocolDelegationDispatchPort, _RaisingDispatchPort())
    )


def _request(**extra: object) -> ModelDelegateSkillRequest:
    return ModelDelegateSkillRequest.model_validate(
        {
            "prompt": "summarise the log",
            "task_type": "document",
            "source": "claude-code",
            "correlation_id": str(uuid4()),
            **extra,
        }
    )


def test_the_request_declares_and_keeps_both_ids() -> None:
    trace_id, span_id = uuid4(), uuid4()
    request = _request(trace_id=str(trace_id), span_id=str(span_id))
    assert request.trace_id == trace_id
    assert request.span_id == span_id


def test_the_request_without_ids_leaves_them_absent_on_the_wire() -> None:
    request = _request()
    assert request.trace_id is None
    assert request.span_id is None
    wire = request.model_dump(mode="json")
    assert "trace_id" not in wire
    assert "span_id" not in wire


def test_the_terminal_declares_and_keeps_both_ids() -> None:
    trace_id, span_id = uuid4(), uuid4()
    terminal = ModelDelegateSkillResponse.model_validate(
        {
            "correlation_id": str(uuid4()),
            "status": "completed",
            "task_type": "document",
            "trace_id": str(trace_id),
            "span_id": str(span_id),
        }
    )
    assert terminal.trace_id == trace_id
    assert terminal.span_id == span_id


async def test_a_failed_terminal_copies_the_commands_trace_and_span_ids() -> None:
    trace_id, span_id = uuid4(), uuid4()
    request = _request(trace_id=str(trace_id), span_id=str(span_id))

    terminal = await _handler().handle(request)

    assert isinstance(terminal, ModelDelegateSkillFailed)
    assert terminal.trace_id == trace_id
    assert terminal.span_id == span_id
    wire = terminal.model_dump(mode="json")
    assert wire["trace_id"] == str(trace_id)
    assert wire["span_id"] == str(span_id)


async def test_two_commands_carry_their_own_ids() -> None:
    first = _request(trace_id=str(uuid4()), span_id=str(uuid4()))
    second = _request(trace_id=str(uuid4()), span_id=str(uuid4()))
    handler = _handler()

    first_terminal = await handler.handle(first)
    second_terminal = await handler.handle(second)

    assert first_terminal.trace_id == first.trace_id
    assert second_terminal.trace_id == second.trace_id
    assert first_terminal.trace_id != second_terminal.trace_id


async def test_a_command_without_ids_yields_a_terminal_without_ids() -> None:
    """Control: nothing is invented where the command carried no ids."""
    terminal = await _handler().handle(_request())

    assert terminal.trace_id is None
    assert terminal.span_id is None
    wire = terminal.model_dump(mode="json")
    assert "trace_id" not in wire
    assert "span_id" not in wire


def test_a_non_uuid_trace_id_is_refused() -> None:
    with pytest.raises(ValidationError):
        _request(trace_id="not-a-uuid")


def test_a_misspelt_key_is_still_refused() -> None:
    with pytest.raises(ValidationError) as excinfo:
        _request(trace_idd=str(UUID(int=1)))
    assert any(
        error["loc"] == ("trace_idd",) and error["type"] == "extra_forbidden"
        for error in excinfo.value.errors()
    )
