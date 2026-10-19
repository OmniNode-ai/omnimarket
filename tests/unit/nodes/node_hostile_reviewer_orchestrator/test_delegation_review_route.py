# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The hostile reviewer's delegation route and its finding payloads (OMN-17427)."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Literal, cast
from uuid import uuid4

import pytest

from omnimarket.models.delegation.wire.model_delegate_skill_request import (
    ModelDelegateSkillRequest,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.handler_delegate_skill import (
    HandlerDelegateSkill,
)
from omnimarket.nodes.node_hostile_reviewer_orchestrator.handlers.handler_delegation_review_inference import (
    HandlerDelegationReviewInference,
)
from omnimarket.nodes.node_hostile_reviewer_orchestrator.handlers.handler_hostile_reviewer_orchestrator import (
    HandlerHostileReviewerOrchestrator,
)
from omnimarket.nodes.node_hostile_reviewer_orchestrator.models.model_hostile_reviewer_start_command import (
    ModelHostileReviewerStartCommand,
)

pytestmark = pytest.mark.unit


class _RecordingHandler:
    def __init__(self, *, status: str = "completed", response: str = "[]") -> None:
        self.requests: list[ModelDelegateSkillRequest] = []
        self._answer = SimpleNamespace(
            status=status, response=response, error_message="refused by routing"
        )

    async def handle(self, request: ModelDelegateSkillRequest) -> SimpleNamespace:
        self.requests.append(request)
        return self._answer


@pytest.mark.asyncio
async def test_review_call_is_a_review_class_delegation_pinned_to_the_route() -> None:
    handler = _RecordingHandler(response="reviewed")
    adapter = HandlerDelegationReviewInference(
        handler=cast("HandlerDelegateSkill", handler)
    )
    text = await adapter.infer(
        model_key="local-heavy-reasoning",
        system_prompt="be hostile",
        user_prompt="diff text",
        timeout_seconds=120.5,
        temperature=0.0,
    )
    assert text == "reviewed"
    (request,) = handler.requests
    assert request.task_type == "review"
    assert request.backend_id == "local-heavy-reasoning"
    assert request.prompt == "diff text"
    assert request.system_prompt == "be hostile"
    assert request.requested_timeout_seconds == 120
    assert request.wait is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "response"), [("failed", "partial"), ("completed", "")]
)
async def test_a_delegation_that_does_not_complete_fails_its_route(
    status: str, response: str
) -> None:
    adapter = HandlerDelegationReviewInference(
        handler=cast(
            "HandlerDelegateSkill",
            _RecordingHandler(status=status, response=response),
        )
    )
    with pytest.raises(RuntimeError, match="did not complete"):
        await adapter.infer("route-a", "system", "user", 30.0)


def test_the_start_command_selects_the_delegation_route() -> None:
    def command(
        **extra: Literal["bridge", "delegation"],
    ) -> ModelHostileReviewerStartCommand:
        return ModelHostileReviewerStartCommand(
            correlation_id=uuid4(),
            repo="OmniNode-ai/test",
            pr_number=1,
            models=["route-a"],
            requested_at=datetime.now(tz=UTC),
            **extra,
        )

    orchestrator = HandlerHostileReviewerOrchestrator()
    assert command().route == "bridge"
    assert isinstance(
        orchestrator._resolve_adapter(command(route="delegation")),
        HandlerDelegationReviewInference,
    )
