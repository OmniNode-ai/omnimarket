# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""DI injection-path tests for OMN-10872.

Verifies that HandlerNavigationHistoryReducer accepts a ProtocolNavigationHistoryWriter
stub and routes all calls through it instead of constructing a concrete writer.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from omnimarket.nodes.node_navigation_history_reducer import (
    HandlerNavigationHistoryReducer,
    HandlerNavigationHistoryWriter,
    ModelNavigationHistoryRequest,
    ModelNavigationSession,
    ProtocolNavigationHistoryWriter,
)
from omnimarket.nodes.node_navigation_history_reducer.models.model_navigation_history_response import (
    ModelNavigationHistoryResponse,
)
from omnimarket.nodes.node_navigation_history_reducer.models.model_navigation_session import (
    ModelNavigationOutcomeSuccess,
    ModelPlanStep,
)


def _make_session() -> ModelNavigationSession:
    step = ModelPlanStep(
        step_index=0,
        from_state_id="s0",
        to_state_id="s1",
        action="move",
        executed_at=datetime.now(tz=UTC),
    )
    return ModelNavigationSession(
        session_id=uuid4(),
        goal_condition="reach s1",
        start_state_id="s0",
        end_state_id="s1",
        executed_steps=[step],
        final_outcome=ModelNavigationOutcomeSuccess(reached_state_id="s1"),
        graph_fingerprint="fp123",
        created_at=datetime.now(tz=UTC),
    )


class _StubWriter:
    """Minimal ProtocolNavigationHistoryWriter stub for injection tests."""

    def __init__(self) -> None:
        self.record_calls: list[ModelNavigationSession] = []
        self.close_calls: int = 0

    async def record(
        self, session: ModelNavigationSession
    ) -> ModelNavigationHistoryResponse:
        self.record_calls.append(session)
        return ModelNavigationHistoryResponse(
            session_id=session.session_id,
            status="success",
            postgres_written=True,
            qdrant_written=True,
        )

    async def close(self) -> None:
        self.close_calls += 1


@pytest.mark.unit
class TestNavigationHistoryReducerDiInjection:
    """OMN-10872: handler accepts ProtocolNavigationHistoryWriter via DI."""

    def test_protocol_is_runtime_checkable(self) -> None:
        stub = _StubWriter()
        assert isinstance(stub, ProtocolNavigationHistoryWriter)

    def test_concrete_writer_satisfies_protocol(self) -> None:
        writer = HandlerNavigationHistoryWriter.__new__(HandlerNavigationHistoryWriter)
        assert isinstance(writer, ProtocolNavigationHistoryWriter)

    @pytest.mark.asyncio
    async def test_injected_writer_is_used(self) -> None:
        stub = _StubWriter()
        handler = HandlerNavigationHistoryReducer(writer=stub)
        await handler.initialize()

        session = _make_session()
        request = ModelNavigationHistoryRequest(session=session)
        result = await handler.execute(request)

        assert len(stub.record_calls) == 1
        assert stub.record_calls[0].session_id == session.session_id
        assert result.status == "success"

    @pytest.mark.asyncio
    async def test_shutdown_calls_writer_close(self) -> None:
        stub = _StubWriter()
        handler = HandlerNavigationHistoryReducer(writer=stub)
        await handler.initialize()
        await handler.shutdown()

        assert stub.close_calls == 1

    @pytest.mark.asyncio
    async def test_default_construction_still_works(self) -> None:
        """No writer injected — concrete HandlerNavigationHistoryWriter is built."""
        handler = HandlerNavigationHistoryReducer(
            pg_dsn="postgresql://localhost/test",
            qdrant_host="localhost",
        )
        assert handler._writer is not None
        assert isinstance(handler._writer, HandlerNavigationHistoryWriter)

    @pytest.mark.asyncio
    async def test_uninitialized_handler_returns_error(self) -> None:
        stub = _StubWriter()
        handler = HandlerNavigationHistoryReducer(writer=stub)
        session = _make_session()
        request = ModelNavigationHistoryRequest(session=session)

        result = await handler.execute(request)
        assert result.status == "error"
        assert "not initialized" in (result.error_message or "").lower()
        assert len(stub.record_calls) == 0
