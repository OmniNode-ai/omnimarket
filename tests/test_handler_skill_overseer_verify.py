# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for the container-driven HandlerSkillRequested (OMN-13603).

The handler was converted from a function-form handler to a class whose
constructor takes the injectable container so the runtime resolver builds it at
boot. The polymorphic-agent TaskDispatcher is resolved at the dispatch boundary;
absent a dispatcher the handler returns a structured FAILED result.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from omnimarket.nodes.node_skill_overseer_verify_orchestrator.handlers.handler_skill_requested import (
    HandlerSkillRequested,
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
        dispatcher = AsyncMock(return_value="work done\nRESULT:\nstatus: success\n")
        handler = HandlerSkillRequested(
            container=MagicMock(), task_dispatcher=dispatcher
        )
        result = await handler.handle(_request())
        dispatcher.assert_awaited_once()
        assert result.status == SkillResultStatus.SUCCESS
        assert result.error is None

    @pytest.mark.asyncio
    async def test_dispatch_parses_failed_result_block(self) -> None:
        dispatcher = AsyncMock(
            return_value="RESULT:\nstatus: failed\nerror: gate rejected\n"
        )
        handler = HandlerSkillRequested(
            container=MagicMock(), task_dispatcher=dispatcher
        )
        result = await handler.handle(_request())
        assert result.status == SkillResultStatus.FAILED
        assert result.error == "gate rejected"

    @pytest.mark.asyncio
    async def test_dispatcher_exception_returns_failed(self) -> None:
        dispatcher = AsyncMock(side_effect=RuntimeError("agent unavailable"))
        handler = HandlerSkillRequested(
            container=MagicMock(), task_dispatcher=dispatcher
        )
        result = await handler.handle(_request())
        assert result.status == SkillResultStatus.FAILED
        assert result.error == "task_dispatcher raised an exception"
