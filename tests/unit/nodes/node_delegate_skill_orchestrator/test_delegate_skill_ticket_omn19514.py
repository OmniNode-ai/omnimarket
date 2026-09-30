# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The delegate-skill terminal carries the request's ticket (OMN-19514, step 2)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from omnimarket.models.delegation.wire.model_delegate_skill_response import (
    ModelDelegateSkillCompleted,
    ModelDelegateSkillFailed,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.handler_delegate_skill import (
    HandlerDelegateSkill,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.models.model_delegate_skill_request import (
    ModelDelegateSkillRequest,
)


def _port(
    result: dict[str, object] | None = None, exc: Exception | None = None
) -> AsyncMock:
    port = AsyncMock()
    if exc is not None:
        port.dispatch.side_effect = exc
    else:
        port.dispatch.return_value = result or {
            "status": "completed",
            "content": "ok",
            "quality_gate_passed": True,
        }
    return port


def _request(metadata: dict[str, str] | None = None) -> ModelDelegateSkillRequest:
    return ModelDelegateSkillRequest(
        prompt="Summarize the router",
        task_type="summarization",
        source="claude-code",
        metadata=metadata or {},
    )


@pytest.mark.unit
async def test_a_completed_terminal_carries_the_request_ticket() -> None:
    port = _port()
    handler = HandlerDelegateSkill(object(), dispatch_port=port)
    request = _request(metadata={"ticket_id": "OMN-19514"})
    terminal = await handler.handle(request)
    assert isinstance(terminal, ModelDelegateSkillCompleted)
    assert terminal.ticket_id == "OMN-19514"
    assert terminal.model_dump(mode="json")["ticket_id"] == "OMN-19514"


@pytest.mark.unit
async def test_a_failed_terminal_carries_the_request_ticket() -> None:
    port = _port(exc=RuntimeError("boom"))
    handler = HandlerDelegateSkill(object(), dispatch_port=port)
    request = _request(metadata={"ticket_id": "OMN-19514"})
    terminal = await handler.handle(request)
    assert isinstance(terminal, ModelDelegateSkillFailed)
    assert terminal.ticket_id == "OMN-19514"


@pytest.mark.unit
async def test_no_ticket_means_no_key_on_the_wire() -> None:
    port = _port()
    handler = HandlerDelegateSkill(object(), dispatch_port=port)
    request = _request(metadata={})
    terminal = await handler.handle(request)
    assert terminal.ticket_id is None
    assert "ticket_id" not in terminal.model_dump(mode="json")


@pytest.mark.unit
async def test_a_malformed_ticket_is_not_guessed_into_one() -> None:
    port = _port()
    handler = HandlerDelegateSkill(object(), dispatch_port=port)
    request = _request(metadata={"ticket_id": "omn-19514"})
    terminal = await handler.handle(request)
    assert terminal.ticket_id is None
    assert "ticket_id" not in terminal.model_dump(mode="json")
    assert isinstance(terminal, ModelDelegateSkillCompleted)


@pytest.mark.unit
async def test_the_ticket_is_not_forwarded_as_a_prompt_or_session() -> None:
    port = _port()
    handler = HandlerDelegateSkill(object(), dispatch_port=port)
    request = _request(metadata={"ticket_id": "OMN-19514"})
    await handler.handle(request)
    kwargs = port.dispatch.await_args.kwargs
    assert kwargs["prompt"] == "Summarize the router"
    assert kwargs["source_session_id"] is None
