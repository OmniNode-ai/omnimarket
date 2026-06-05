# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain tests for node_delegate_skill_orchestrator (OMN-12704).

Exercises the delegation route end-to-end through the node handler with a stub
dispatch port (no network): a typed ModelDelegateSkillRequest flows through
HandlerDelegateSkill and yields a typed ModelDelegateSkillResponse that preserves
the route's quality-gate result, model/backend selection, correlation id, and
output content. This is the authority that node_task_execution_orchestrator
composes for coding/refactor/review work; the parity is asserted here so the
delegation route stays the single owner of delegation success/failure.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from omnimarket.models.delegation.wire.model_delegate_skill_request import (
    ModelDelegateSkillRequest,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.handler_delegate_skill import (
    HandlerDelegateSkill,
)


class _StubDispatchPort:
    """In-process delegation dispatch port returning typed evidence (no network)."""

    def __init__(self, result: dict[str, object]) -> None:
        self._result = result
        self.calls: list[dict[str, object]] = []

    async def dispatch(
        self,
        *,
        prompt: str,
        task_type: str,
        correlation_id: UUID,
        max_tokens: int,
        source_file_path: str | None,
        source_session_id: str | None,
        wait: bool,
        quality_contract_mode: str,
        acceptance_criteria: tuple[str, ...],
    ) -> dict[str, object]:
        self.calls.append(
            {
                "prompt": prompt,
                "task_type": task_type,
                "correlation_id": correlation_id,
            }
        )
        return self._result


@pytest.mark.unit
class TestDelegateSkillGoldenChain:
    """Request -> HandlerDelegateSkill -> typed response chain (stubbed port)."""

    async def test_completed_delegation_preserves_route_evidence(self) -> None:
        correlation_id = uuid4()
        port = _StubDispatchPort(
            {
                "status": "completed",
                "content": "def parse(): ...",
                "delegated_to": "local-runtime",
                "model_name": "qwen-coder",
                "quality_gate_passed": True,
                "quality_score": 0.91,
            }
        )
        handler = HandlerDelegateSkill(dispatch_port=port)

        response = await handler.handle(
            ModelDelegateSkillRequest(
                prompt="generate a parser for the config file",
                task_type="code_generation",
                source="claude-code",
                correlation_id=correlation_id,
            )
        )

        assert response.status == "completed"
        assert response.correlation_id == correlation_id
        assert response.task_type == "code_generation"
        assert response.provider == "local-runtime"
        assert response.model_name == "qwen-coder"
        assert response.response == "def parse(): ..."
        assert response.quality_gate_passed is True
        # The route dispatched exactly the requested coding work.
        assert port.calls[0]["task_type"] == "code_generation"
        assert port.calls[0]["prompt"] == "generate a parser for the config file"

    async def test_failed_dispatch_stays_typed_failure(self) -> None:
        """A dispatch exception is surfaced as a typed failed response by the route."""

        class _RaisingPort:
            async def dispatch(self, **_: object) -> dict[str, object]:
                raise RuntimeError("backend unavailable")

        handler = HandlerDelegateSkill(dispatch_port=_RaisingPort())
        correlation_id = uuid4()

        response = await handler.handle(
            ModelDelegateSkillRequest(
                prompt="do work",
                task_type="code_generation",
                source="claude-code",
                correlation_id=correlation_id,
            )
        )

        assert response.status == "failed"
        assert response.correlation_id == correlation_id
        assert "backend unavailable" in response.error_message
