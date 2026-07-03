# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for the container-driven HandlerSkillRequested (OMN-13603).

The handler was converted from a function-form handler to a class whose
constructor takes the injectable container so the runtime resolver builds it at
boot. The polymorphic-agent TaskDispatcher is resolved at the dispatch boundary;
absent a dispatcher the handler returns a structured FAILED result.
"""

from __future__ import annotations

from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from omnimarket.nodes.node_skill_overseer_verify_orchestrator.handlers.handler_skill_requested import (
    HandlerSkillRequested,
    TaskDispatcher,
)
from omnimarket.nodes.node_skill_overseer_verify_orchestrator.models import (
    ModelSkillRequest,
    SkillResultStatus,
)


def _request() -> ModelSkillRequest:
    return ModelSkillRequest(
        skill_name="overseer_verify",
        skill_path="/skills/overseer_verify/SKILL.md",
        args={"ticket": "OMN-13603"},
        correlation_id=uuid4(),
    )


def _dispatcher_returning(output: str) -> tuple[TaskDispatcher, list[str]]:
    """Build a real, typed ``TaskDispatcher`` (agent-dispatch callable) that
    returns ``output`` and records the prompts it was awaited with.

    The task_dispatcher contract is ``Callable[[str], Awaitable[str]]`` — a real
    async closure satisfies it exactly, so the agent-dispatch boundary is
    exercised through its actual type rather than a bare ``AsyncMock``. This is
    the agent-spawn surface, not the platform model-router/inference boundary.
    """
    calls: list[str] = []

    async def _dispatch(prompt: str) -> str:
        calls.append(prompt)
        return output

    return _dispatch, calls


def _dispatcher_raising(exc: Exception) -> TaskDispatcher:
    """Real, typed ``TaskDispatcher`` that raises to exercise the failure path."""

    async def _dispatch(prompt: str) -> str:
        raise exc

    return _dispatch


@pytest.mark.unit
class TestHandlerSkillRequested:
    def test_constructor_requires_container(self) -> None:
        """Container is a required (non-default) constructor param — mandatory DI."""
        with pytest.raises(TypeError):
            HandlerSkillRequested()  # type: ignore[call-arg]

    def test_constructible_from_container_alone(self) -> None:
        """Boot-time construction with only the injectable container must succeed."""
        handler = HandlerSkillRequested(container=MagicMock())
        assert handler is not None

    @pytest.mark.asyncio
    async def test_no_dispatcher_returns_failed(self) -> None:
        """Without a dispatcher the handler returns FAILED, never crashes."""
        handler = HandlerSkillRequested(container=MagicMock())
        result = await handler.handle(_request())
        assert result.status == SkillResultStatus.FAILED
        assert result.error is not None
        assert "dispatcher" in result.error

    @pytest.mark.asyncio
    async def test_dispatch_parses_success_result_block(self) -> None:
        """An explicit dispatcher's RESULT: block is parsed to SUCCESS."""
        dispatcher, calls = _dispatcher_returning(
            "work done\nRESULT:\nstatus: success\n"
        )
        handler = HandlerSkillRequested(
            container=MagicMock(), task_dispatcher=dispatcher
        )
        result = await handler.handle(_request())
        assert len(calls) == 1
        assert result.status == SkillResultStatus.SUCCESS
        assert result.error is None

    @pytest.mark.asyncio
    async def test_dispatch_parses_failed_result_block(self) -> None:
        dispatcher, _calls = _dispatcher_returning(
            "RESULT:\nstatus: failed\nerror: gate rejected\n"
        )
        handler = HandlerSkillRequested(
            container=MagicMock(), task_dispatcher=dispatcher
        )
        result = await handler.handle(_request())
        assert result.status == SkillResultStatus.FAILED
        assert result.error == "gate rejected"

    @pytest.mark.asyncio
    async def test_dispatcher_exception_returns_failed(self) -> None:
        dispatcher = _dispatcher_raising(RuntimeError("agent unavailable"))
        handler = HandlerSkillRequested(
            container=MagicMock(), task_dispatcher=dispatcher
        )
        result = await handler.handle(_request())
        assert result.status == SkillResultStatus.FAILED
        assert result.error == "task_dispatcher raised an exception"
