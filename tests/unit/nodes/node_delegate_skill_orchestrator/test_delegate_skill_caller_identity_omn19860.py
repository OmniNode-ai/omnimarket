# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Every delegate-skill terminal carries its caller's identity (OMN-19860)."""

from __future__ import annotations

import asyncio
from uuid import UUID

import pytest

from omnimarket.inference.task_class_authority import ModelTaskClassExecutionBudget
from omnimarket.models.delegation.wire.model_delegate_skill_response import (
    ModelDelegateSkillCompleted,
    ModelDelegateSkillFailed,
    ModelDelegateSkillResponse,
)
from omnimarket.models.delegation.wire.model_delegate_skill_terminal_projection import (
    ModelDelegateSkillTerminalProjection,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.handlers import (
    handler_delegate_skill as handler_delegate_skill_module,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.handler_delegate_skill import (
    HandlerDelegateSkill,
)
from omnimarket.nodes.node_projection_delegation.handlers.handler_delegation_caller_lane_fold import (
    HandlerDelegationCallerLaneFold,
)

from .test_delegate_skill_ticket_omn19514 import _port, _request

pytestmark = pytest.mark.unit

_LANE = "omn-19860-terminal-carries-caller-lane"
_SESSION = UUID("b57923e3-8cc6-4f7a-b8b8-0e3e4c5f3086")
_IDENTITY = {"caller_lane": _LANE, "session_id": str(_SESSION)}


async def test_a_completed_terminal_carries_the_request_identity() -> None:
    request = _request({"caller_lane": _LANE, "ticket_id": "OMN-19860"}).model_copy(
        update={"session_id": _SESSION.hex.upper()}
    )
    terminal = await HandlerDelegateSkill(object(), dispatch_port=_port()).handle(
        request
    )
    assert isinstance(terminal, ModelDelegateSkillCompleted)
    assert terminal.caller_lane == _LANE
    assert terminal.session_id == str(_SESSION)
    assert terminal.ticket_id == "OMN-19860"
    payload = terminal.model_dump(mode="json")
    assert payload["caller_lane"] == _LANE
    assert payload["session_id"] == str(_SESSION)


async def test_a_timeout_terminal_carries_the_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        handler_delegate_skill_module,
        "resolve_task_class_execution_budget",
        lambda _task_type: ModelTaskClassExecutionBudget(
            task_class_timeout_ceiling_seconds=1,
            terminal_delivery_margin_seconds=1,
        ),
    )
    cancelled = asyncio.Event()

    async def never_return(**_kwargs: object) -> dict[str, object]:
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
        raise AssertionError("dispatch must be cancelled on timeout")

    port = _port()
    port.dispatch.side_effect = never_return
    terminal = await HandlerDelegateSkill(object(), dispatch_port=port).handle(
        _request(_IDENTITY)
    )
    assert isinstance(terminal, ModelDelegateSkillFailed)
    assert terminal.status == "timeout"
    assert cancelled.is_set()
    assert terminal.caller_lane == _LANE
    assert terminal.session_id == str(_SESSION)


@pytest.mark.parametrize("exc", [RuntimeError("boom"), asyncio.CancelledError()])
async def test_failure_and_shutdown_terminals_carry_the_identity(
    exc: BaseException,
) -> None:
    port = _port()
    port.dispatch.side_effect = exc
    terminal = await HandlerDelegateSkill(object(), dispatch_port=port).handle(
        _request(_IDENTITY)
    )
    assert isinstance(terminal, ModelDelegateSkillFailed)
    assert terminal.status == "failed"
    assert terminal.caller_lane == _LANE
    assert terminal.session_id == str(_SESSION)


async def test_a_budget_refusal_carries_the_identity() -> None:
    port = _port()
    request = _request(_IDENTITY).model_copy(
        update={"requested_timeout_seconds": 100000}
    )
    terminal = await HandlerDelegateSkill(object(), dispatch_port=port).handle(request)
    assert isinstance(terminal, ModelDelegateSkillFailed)
    assert terminal.budget_refusal is not None
    assert terminal.caller_lane == _LANE
    assert terminal.session_id == str(_SESSION)
    port.dispatch.assert_not_awaited()


async def test_a_malformed_lane_is_dropped_without_refusing_the_delegation(
    caplog: pytest.LogCaptureFixture,
) -> None:
    request = _request({**_IDENTITY, "caller_lane": "has space|pipe"})
    terminal = await HandlerDelegateSkill(object(), dispatch_port=_port()).handle(
        request
    )
    assert isinstance(terminal, ModelDelegateSkillCompleted)
    assert terminal.caller_lane is None
    assert terminal.session_id == str(_SESSION)
    assert "caller_lane" not in terminal.model_dump(mode="json")
    assert "caller lane refused" in caplog.text
    assert str(request.correlation_id) in caplog.text


@pytest.mark.parametrize("in_metadata", [False, True])
async def test_a_non_uuid_session_is_dropped(
    in_metadata: bool,
    caplog: pytest.LogCaptureFixture,
) -> None:
    request = _request({"caller_lane": _LANE})
    if in_metadata:
        request = request.model_copy(
            update={"metadata": {"caller_lane": _LANE, "session_id": "not-a-uuid"}}
        )
    else:
        request = request.model_copy(update={"session_id": "not-a-uuid"})
    terminal = await HandlerDelegateSkill(object(), dispatch_port=_port()).handle(
        request
    )
    assert isinstance(terminal, ModelDelegateSkillCompleted)
    assert terminal.caller_lane == _LANE
    assert terminal.session_id is None
    assert "session_id" not in terminal.model_dump(mode="json")
    assert "session refused" in caplog.text
    assert str(request.correlation_id) in caplog.text


async def test_the_request_session_takes_precedence_over_metadata() -> None:
    request = _request({"session_id": "not-a-uuid"}).model_copy(
        update={"session_id": str(_SESSION)}
    )
    terminal = await HandlerDelegateSkill(object(), dispatch_port=_port()).handle(
        request
    )
    assert terminal.session_id == str(_SESSION)


async def test_no_identity_means_no_keys_on_the_wire() -> None:
    terminal = await HandlerDelegateSkill(object(), dispatch_port=_port()).handle(
        _request()
    )
    assert terminal.caller_lane is None
    assert terminal.session_id is None
    payload = terminal.model_dump(mode="json")
    assert "caller_lane" not in payload
    assert "session_id" not in payload


async def test_the_terminal_round_trips_into_the_lane_projection() -> None:
    terminal = await HandlerDelegateSkill(object(), dispatch_port=_port()).handle(
        _request(_IDENTITY)
    )
    projection = ModelDelegateSkillTerminalProjection.from_payload(
        terminal.model_dump(mode="json")
    )
    assert projection.session_id == _SESSION
    assert HandlerDelegationCallerLaneFold().handle(projection).row_columns() == {
        "caller_lane": _LANE
    }


@pytest.mark.parametrize("session", [_SESSION, _SESSION.hex.upper(), str(_SESSION)])
def test_the_response_stores_a_canonical_uuid_string(session: object) -> None:
    response = ModelDelegateSkillResponse.model_validate(
        {
            "status": "completed",
            "correlation_id": _SESSION,
            "task_type": "summarization",
            "session_id": session,
        }
    )
    assert response.session_id == str(_SESSION)
    assert response.model_dump(mode="json")["session_id"] == str(_SESSION)


@pytest.mark.parametrize(
    "session", ["not-a-uuid", 17, b"b57923e38cc64f7ab8b80e3e4c5f3086", []]
)
def test_the_response_drops_an_invalid_session_without_refusing(
    session: object,
) -> None:
    """Attribution, not policy: a malformed session never dead-letters a terminal."""
    response = ModelDelegateSkillResponse.model_validate(
        {
            "status": "completed",
            "correlation_id": _SESSION,
            "task_type": "summarization",
            "session_id": session,
        }
    )
    assert response.session_id is None
    assert "session_id" not in response.model_dump(mode="json")
