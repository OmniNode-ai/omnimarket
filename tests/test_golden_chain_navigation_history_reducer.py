# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain tests for node_navigation_history_reducer.

Verifies that navigation session models round-trip cleanly and that
the handler and node can be imported from omnimarket.nodes.

Related: OMN-8301 (Wave 5 migration), OMN-2584
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
    ModelPlanStep,
    NodeNavigationHistoryReducer,
)


def _make_success_session() -> ModelNavigationSession:
    from omnimarket.nodes.node_navigation_history_reducer.models import (
        ModelNavigationOutcomeSuccess,
    )

    step = ModelPlanStep(
        step_index=0,
        from_state_id="s0",
        to_state_id="s1",
        action="move forward",
        executed_at=datetime.now(tz=UTC),
    )
    success_outcome = ModelNavigationOutcomeSuccess(reached_state_id="s1")
    return ModelNavigationSession(
        session_id=uuid4(),
        goal_condition="reach s1",
        start_state_id="s0",
        end_state_id="s1",
        executed_steps=[step],
        final_outcome=success_outcome,
        graph_fingerprint="abc123",
        created_at=datetime.now(tz=UTC),
    )


@pytest.mark.unit
class TestNavigationHistoryReducerGoldenChain:
    """Golden chain: navigation history reducer node contracts."""

    def test_model_request_roundtrip(self) -> None:
        """ModelNavigationHistoryRequest serializes cleanly."""
        session = _make_success_session()
        request = ModelNavigationHistoryRequest(session=session)
        assert request.session.session_id == session.session_id

    def test_model_request_frozen(self) -> None:
        """ModelNavigationHistoryRequest is immutable."""
        from pydantic import ValidationError

        session = _make_success_session()
        request = ModelNavigationHistoryRequest(session=session)
        with pytest.raises(ValidationError):
            request.session = _make_success_session()  # type: ignore[misc]

    def test_plan_step_roundtrip(self) -> None:
        """ModelPlanStep serializes cleanly with all fields."""
        step = ModelPlanStep(
            step_index=2,
            from_state_id="a",
            to_state_id="b",
            action="jump",
            executed_at=datetime.now(tz=UTC),
            metadata={"weight": 1.5, "reliable": True},
        )
        assert step.step_index == 2
        assert step.metadata is not None
        assert step.metadata["weight"] == 1.5

    def test_plan_step_frozen(self) -> None:
        """ModelPlanStep is immutable."""
        from pydantic import ValidationError

        step = ModelPlanStep(
            step_index=0,
            from_state_id="a",
            to_state_id="b",
            action="test",
            executed_at=datetime.now(tz=UTC),
        )
        with pytest.raises(ValidationError):
            step.step_index = 99  # type: ignore[misc]

    def test_handler_importable(self) -> None:
        """HandlerNavigationHistoryReducer is importable from omnimarket.nodes."""
        assert HandlerNavigationHistoryReducer is not None

    def test_writer_importable(self) -> None:
        """HandlerNavigationHistoryWriter is importable from omnimarket.nodes."""
        assert HandlerNavigationHistoryWriter is not None

    def test_node_class_importable(self) -> None:
        """NodeNavigationHistoryReducer is importable from omnimarket.nodes."""
        assert NodeNavigationHistoryReducer is not None

    def test_session_has_required_fields(self) -> None:
        """ModelNavigationSession requires session_id, goal_condition, and outcome."""
        session = _make_success_session()
        assert session.session_id is not None
        assert session.goal_condition == "reach s1"
        assert session.executed_steps is not None
        assert len(session.executed_steps) == 1
        assert session.graph_fingerprint == "abc123"
