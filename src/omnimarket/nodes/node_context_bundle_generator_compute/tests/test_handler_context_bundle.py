"""Tests for HandlerContextBundle — deterministic context bundle generation."""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_context_bundle_generator_compute.handlers.handler_context_bundle import (
    HandlerContextBundle,
)
from omnimarket.nodes.node_context_bundle_generator_compute.models.model_bundle_request import (
    ModelContextBundleRequest,
)
from omnimarket.nodes.node_context_bundle_generator_compute.models.model_bundle_result import (
    EnumBundleStatus,
)
from omnimarket.nodes.node_context_bundle_generator_compute.models.model_context_bundle import (
    EnumContextLevel,
    ModelContextBundleL0,
    ModelContextBundleL1,
    ModelContextBundleL2,
    ModelContextBundleL3,
    ModelContextBundleL4,
)
from omnimarket.nodes.node_context_bundle_generator_compute.models.model_run_context import (
    ModelRunContext,
)
from omnimarket.nodes.node_context_bundle_generator_compute.models.model_task_state import (
    EnumTaskPriority,
    EnumTaskStatus,
    ModelTaskState,
)


def _task(
    *,
    ticket_id: str = "OMN-8051",
    title: str = "Build context bundle generator",
    status: EnumTaskStatus = EnumTaskStatus.IN_PROGRESS,
    assignee: str = "agent-alpha",
    priority: EnumTaskPriority = EnumTaskPriority.HIGH,
    labels: tuple[str, ...] = ("compute", "context"),
    parent_ticket_id: str = "OMN-8000",
    related_ticket_ids: tuple[str, ...] = ("OMN-8040", "OMN-8050"),
) -> ModelTaskState:
    return ModelTaskState(
        ticket_id=ticket_id,
        title=title,
        status=status,
        assignee=assignee,
        priority=priority,
        labels=labels,
        parent_ticket_id=parent_ticket_id,
        related_ticket_ids=related_ticket_ids,
    )


def _run(
    *,
    session_id: str = "sess-001",
    agent_id: str = "agent-alpha",
    timestamp: str = "2026-05-09T00:00:00Z",
    worker_type: str = "ticket_worker",
    repo: str = "omnimarket",
    branch: str = "jonah/omn-8051-context-bundle-generator",
    trigger_event: str = "onex.cmd.omnimarket.context-bundle-requested.v1",
) -> ModelRunContext:
    return ModelRunContext(
        session_id=session_id,
        agent_id=agent_id,
        timestamp=timestamp,
        worker_type=worker_type,
        repo=repo,
        branch=branch,
        trigger_event=trigger_event,
    )


def _req(
    level: EnumContextLevel = EnumContextLevel.L2,
    historical_summary: str = "",
    prior_attempt_count: int = 0,
) -> ModelContextBundleRequest:
    return ModelContextBundleRequest(
        task_state=_task(),
        run_context=_run(),
        requested_level=level,
        historical_summary=historical_summary,
        prior_attempt_count=prior_attempt_count,
    )


@pytest.mark.unit
class TestHandlerContextBundleStatus:
    def test_status_ok_for_all_levels(self) -> None:
        handler = HandlerContextBundle()
        for level in EnumContextLevel:
            result = handler.handle(_req(level))
            assert result.status == EnumBundleStatus.OK

    def test_no_error_on_success(self) -> None:
        result = HandlerContextBundle().handle(_req())
        assert result.error is None

    def test_bundle_id_is_nonempty(self) -> None:
        result = HandlerContextBundle().handle(_req())
        assert result.bundle_id != ""

    def test_requested_level_echoed(self) -> None:
        result = HandlerContextBundle().handle(_req(EnumContextLevel.L3))
        assert result.requested_level == "L3"

    def test_achieved_level_matches_requested(self) -> None:
        for level in EnumContextLevel:
            result = HandlerContextBundle().handle(_req(level))
            assert result.achieved_level == level.value


@pytest.mark.unit
class TestHandlerContextBundleL0:
    def test_bundle_is_l0_instance(self) -> None:
        result = HandlerContextBundle().handle(_req(EnumContextLevel.L0))
        assert isinstance(result.bundle, ModelContextBundleL0)

    def test_l0_carries_ticket_id(self) -> None:
        result = HandlerContextBundle().handle(_req(EnumContextLevel.L0))
        assert result.bundle.ticket_id == "OMN-8051"

    def test_l0_level_field(self) -> None:
        result = HandlerContextBundle().handle(_req(EnumContextLevel.L0))
        assert result.bundle.level == "L0"


@pytest.mark.unit
class TestHandlerContextBundleL1:
    def test_bundle_is_l1_instance(self) -> None:
        result = HandlerContextBundle().handle(_req(EnumContextLevel.L1))
        assert isinstance(result.bundle, ModelContextBundleL1)

    def test_l1_carries_task_state_fields(self) -> None:
        result = HandlerContextBundle().handle(_req(EnumContextLevel.L1))
        b = result.bundle
        assert isinstance(b, ModelContextBundleL1)
        assert b.title == "Build context bundle generator"
        assert b.status == "in_progress"
        assert b.assignee == "agent-alpha"
        assert b.priority == "high"
        assert "compute" in b.labels

    def test_l1_level_field(self) -> None:
        result = HandlerContextBundle().handle(_req(EnumContextLevel.L1))
        assert result.bundle.level == "L1"


@pytest.mark.unit
class TestHandlerContextBundleL2:
    def test_bundle_is_l2_instance(self) -> None:
        result = HandlerContextBundle().handle(_req(EnumContextLevel.L2))
        assert isinstance(result.bundle, ModelContextBundleL2)

    def test_l2_carries_run_context_fields(self) -> None:
        result = HandlerContextBundle().handle(_req(EnumContextLevel.L2))
        b = result.bundle
        assert isinstance(b, ModelContextBundleL2)
        assert b.session_id == "sess-001"
        assert b.agent_id == "agent-alpha"
        assert b.timestamp == "2026-05-09T00:00:00Z"
        assert b.repo == "omnimarket"
        assert b.branch == "jonah/omn-8051-context-bundle-generator"

    def test_l2_level_field(self) -> None:
        result = HandlerContextBundle().handle(_req(EnumContextLevel.L2))
        assert result.bundle.level == "L2"


@pytest.mark.unit
class TestHandlerContextBundleL3:
    def test_bundle_is_l3_instance(self) -> None:
        result = HandlerContextBundle().handle(_req(EnumContextLevel.L3))
        assert isinstance(result.bundle, ModelContextBundleL3)

    def test_l3_carries_relationship_fields(self) -> None:
        result = HandlerContextBundle().handle(_req(EnumContextLevel.L3))
        b = result.bundle
        assert isinstance(b, ModelContextBundleL3)
        assert b.parent_ticket_id == "OMN-8000"
        assert "OMN-8040" in b.related_ticket_ids
        assert "OMN-8050" in b.related_ticket_ids

    def test_l3_level_field(self) -> None:
        result = HandlerContextBundle().handle(_req(EnumContextLevel.L3))
        assert result.bundle.level == "L3"


@pytest.mark.unit
class TestHandlerContextBundleL4:
    def test_bundle_is_l4_instance(self) -> None:
        result = HandlerContextBundle().handle(
            _req(
                EnumContextLevel.L4,
                historical_summary="prior run succeeded",
                prior_attempt_count=2,
            )
        )
        assert isinstance(result.bundle, ModelContextBundleL4)

    def test_l4_carries_historical_fields(self) -> None:
        result = HandlerContextBundle().handle(
            _req(
                EnumContextLevel.L4,
                historical_summary="prior run succeeded",
                prior_attempt_count=2,
            )
        )
        b = result.bundle
        assert isinstance(b, ModelContextBundleL4)
        assert b.historical_summary == "prior run succeeded"
        assert b.prior_attempt_count == 2

    def test_l4_level_field(self) -> None:
        result = HandlerContextBundle().handle(_req(EnumContextLevel.L4))
        assert result.bundle.level == "L4"

    def test_l4_defaults_for_empty_historical(self) -> None:
        result = HandlerContextBundle().handle(_req(EnumContextLevel.L4))
        b = result.bundle
        assert isinstance(b, ModelContextBundleL4)
        assert b.historical_summary == ""
        assert b.prior_attempt_count == 0


@pytest.mark.unit
class TestHandlerContextBundleDeterminism:
    def test_same_input_same_bundle_id(self) -> None:
        handler = HandlerContextBundle()
        r = _req()
        assert handler.handle(r).bundle_id == handler.handle(r).bundle_id

    def test_different_session_ids_produce_different_bundle_ids(self) -> None:
        handler = HandlerContextBundle()
        req_a = ModelContextBundleRequest(
            task_state=_task(),
            run_context=_run(session_id="sess-aaa"),
            requested_level=EnumContextLevel.L2,
        )
        req_b = ModelContextBundleRequest(
            task_state=_task(),
            run_context=_run(session_id="sess-bbb"),
            requested_level=EnumContextLevel.L2,
        )
        assert handler.handle(req_a).bundle_id != handler.handle(req_b).bundle_id

    def test_different_ticket_ids_produce_different_bundle_ids(self) -> None:
        handler = HandlerContextBundle()
        req_a = ModelContextBundleRequest(
            task_state=_task(ticket_id="OMN-001"),
            run_context=_run(),
            requested_level=EnumContextLevel.L2,
        )
        req_b = ModelContextBundleRequest(
            task_state=_task(ticket_id="OMN-002"),
            run_context=_run(),
            requested_level=EnumContextLevel.L2,
        )
        assert handler.handle(req_a).bundle_id != handler.handle(req_b).bundle_id

    def test_different_levels_produce_different_bundle_ids(self) -> None:
        handler = HandlerContextBundle()
        result_l1 = handler.handle(_req(EnumContextLevel.L1))
        result_l2 = handler.handle(_req(EnumContextLevel.L2))
        assert result_l1.bundle_id != result_l2.bundle_id
