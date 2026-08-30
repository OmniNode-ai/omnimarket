# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden-chain guardrails for node_wave_scheduler_orchestrator [OMN-12210]."""

from __future__ import annotations

from importlib.metadata import entry_points
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_wave_scheduler_orchestrator.handlers.handler_wave_scheduler_orchestrator import (
    HandlerWaveSchedulerOrchestrator,
)
from omnimarket.nodes.node_wave_scheduler_orchestrator.models.model_wave_scheduler_request import (
    ModelWaveSchedulerRequest,
)
from omnimarket.nodes.node_wave_scheduler_orchestrator.models.model_wave_scheduler_result import (
    EnumDependencyViolationKind,
    EnumTicketExecutionStatus,
    EnumWaveSchedulerStatus,
    ModelDependencyViolation,
    ModelWaveAssignment,
    ModelWaveExecutionSummary,
    ModelWaveSchedulerResult,
)

_NODE_NAME = "node_wave_scheduler_orchestrator"
_HANDLER_MODULE = "omnimarket.nodes.node_wave_scheduler_orchestrator.handlers.handler_wave_scheduler_orchestrator"
_HANDLER_CLASS = "HandlerWaveSchedulerOrchestrator"
_REQUEST_MODULE = "omnimarket.nodes.node_wave_scheduler_orchestrator.models.model_wave_scheduler_request"
_REQUEST_CLASS = "ModelWaveSchedulerRequest"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _contract() -> dict:  # type: ignore[type-arg]
    path = _repo_root() / "src" / "omnimarket" / "nodes" / _NODE_NAME / "contract.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    return raw


# ---------------------------------------------------------------------------
# Contract shape
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_wave_scheduler_orchestrator_contract_is_implemented() -> None:
    raw = _contract()

    assert raw["node_not_implemented"] is False
    assert raw["node_type"] == "orchestrator"
    assert raw["handler"]["module"] == _HANDLER_MODULE
    assert raw["handler"]["class"] == _HANDLER_CLASS
    assert raw["handler"]["input_model"] == f"{_REQUEST_MODULE}.{_REQUEST_CLASS}"


@pytest.mark.unit
def test_wave_scheduler_orchestrator_contract_routing_surface() -> None:
    raw = _contract()

    assert raw["handler_routing"]["routing_strategy"] == "operation_match"
    assert raw["handler_routing"]["handlers"] == [
        {
            "operation": "wave_scheduler",
            "handler": {
                "name": _HANDLER_CLASS,
                "module": _HANDLER_MODULE,
            },
        }
    ]


@pytest.mark.unit
def test_wave_scheduler_orchestrator_contract_event_bus() -> None:
    raw = _contract()
    eb = raw["event_bus"]

    assert "consumer_group" not in eb, (
        "OMN-15639: event_bus.consumer_group is seam-deleted. The group name is derived from node identity via compute_consumer_group_id(), never declared."
    )
    assert "onex.cmd.omnimarket.wave-scheduler-start.v1" in eb["subscribe_topics"]
    # OMN-17017: publish_topics is now exactly the terminal event. The four other
    # topics this contract declared (wave-dispatched, wave-completed,
    # stall-detected, dependency-violation) were never published by the node —
    # its Python contained zero publish/Envelope/event_bus references — so the
    # declarations were deleted rather than left as machine-readable claims.
    assert eb["publish_topics"] == ["onex.evt.omnimarket.wave-scheduler-completed.v1"]
    assert "onex.dlq.omnimarket.wave-scheduler.v1" in eb["dlq_topics"]


@pytest.mark.unit
def test_wave_scheduler_orchestrator_terminal_event() -> None:
    raw = _contract()
    assert raw["terminal_event"] == "onex.evt.omnimarket.wave-scheduler-completed.v1"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_wave_scheduler_orchestrator_entry_point_loads() -> None:
    eps = {ep.name: ep for ep in entry_points(group="onex.nodes")}

    loaded = eps[_NODE_NAME].load()

    assert loaded.__name__ == f"omnimarket.nodes.{_NODE_NAME}"


# ---------------------------------------------------------------------------
# Input model (ModelWaveSchedulerRequest)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_model_wave_scheduler_request_minimal() -> None:
    req = ModelWaveSchedulerRequest(plan_path="docs/plans/my-plan.yaml")

    assert req.plan_path == "docs/plans/my-plan.yaml"
    assert req.max_concurrency == 6
    assert req.dry_run is False
    assert req.resume is False
    assert req.fail_fast is False
    assert req.defer_repo_conflicts is False
    assert req.state_dir is None


@pytest.mark.unit
def test_model_wave_scheduler_request_all_flags() -> None:
    req = ModelWaveSchedulerRequest(
        plan_path="/absolute/path/plan.yaml",
        max_concurrency=3,
        dry_run=True,
        resume=True,
        fail_fast=True,
        defer_repo_conflicts=True,
        state_dir="/tmp/onex-state",
    )

    assert req.max_concurrency == 3
    assert req.dry_run is True
    assert req.resume is True
    assert req.fail_fast is True
    assert req.defer_repo_conflicts is True
    assert req.state_dir == "/tmp/onex-state"


@pytest.mark.unit
def test_model_wave_scheduler_request_has_no_healthcheck_config() -> None:
    """OMN-17017: the four-field healthcheck model had zero handler references
    and was not CLI-expressible — unreachable AND inert. It was deleted, not
    re-plumbed; stall detection belongs to node_dispatch_watchdog_orchestrator."""
    assert "healthcheck_config" not in ModelWaveSchedulerRequest.model_fields


@pytest.mark.unit
def test_model_wave_scheduler_request_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        ModelWaveSchedulerRequest(
            plan_path="plan.yaml",
            unexpected_field=True,  # type: ignore[call-arg]
        )


@pytest.mark.unit
def test_model_wave_scheduler_request_is_frozen() -> None:
    req = ModelWaveSchedulerRequest(plan_path="plan.yaml")

    with pytest.raises(ValidationError):
        req.plan_path = "other.yaml"  # type: ignore[misc]


@pytest.mark.unit
def test_model_wave_scheduler_request_max_concurrency_bounds() -> None:
    with pytest.raises(ValidationError):
        ModelWaveSchedulerRequest(plan_path="plan.yaml", max_concurrency=0)

    with pytest.raises(ValidationError):
        ModelWaveSchedulerRequest(plan_path="plan.yaml", max_concurrency=21)

    # boundary values are valid
    req_low = ModelWaveSchedulerRequest(plan_path="plan.yaml", max_concurrency=1)
    req_high = ModelWaveSchedulerRequest(plan_path="plan.yaml", max_concurrency=20)
    assert req_low.max_concurrency == 1
    assert req_high.max_concurrency == 20


# ---------------------------------------------------------------------------
# Output models
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_model_wave_scheduler_result_minimal() -> None:
    result = ModelWaveSchedulerResult(
        plan_path="plan.yaml",
        run_status=EnumWaveSchedulerStatus.COMPLETED,
    )

    assert result.plan_path == "plan.yaml"
    assert result.run_status == EnumWaveSchedulerStatus.COMPLETED
    assert result.wave_assignments == ()
    assert result.wave_execution_summaries == ()
    assert result.dependency_violations == ()
    assert result.total_tickets == 0
    assert result.tickets_completed == 0
    assert result.tickets_failed == 0
    assert result.tickets_blocked == 0
    assert result.tickets_unreported == 0
    assert result.tickets_skipped == 0
    assert result.dispatch_lifecycle_path is None
    assert result.dry_run is False
    assert result.resumed is False


@pytest.mark.unit
def test_model_wave_scheduler_result_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        ModelWaveSchedulerResult(
            plan_path="plan.yaml",
            run_status=EnumWaveSchedulerStatus.COMPLETED,
            bogus_field=True,  # type: ignore[call-arg]
        )


@pytest.mark.unit
def test_model_wave_assignment_fields() -> None:
    assignment = ModelWaveAssignment(
        wave_id=0,
        ticket_ids=("OMN-001", "OMN-002", "OMN-003"),
        repo_assignments=(("OMN-001", "omniclaude"), ("OMN-002", "omnibase_core")),
    )

    assert assignment.wave_id == 0
    assert len(assignment.ticket_ids) == 3
    assert assignment.deferred_ticket_ids == ()


@pytest.mark.unit
def test_model_wave_execution_summary_fields() -> None:
    summary = ModelWaveExecutionSummary(
        wave_id=1,
        dispatched_count=4,
        completed_count=3,
        failed_count=1,
        blocked_count=0,
        stalled_count=0,
        skipped_count=0,
        ticket_statuses=(
            ("OMN-010", EnumTicketExecutionStatus.COMPLETED),
            ("OMN-011", EnumTicketExecutionStatus.FAILED),
        ),
    )

    assert summary.wave_id == 1
    assert summary.dispatched_count == 4
    assert summary.failed_count == 1
    assert len(summary.ticket_statuses) == 2


@pytest.mark.unit
def test_model_dependency_violation_cycle() -> None:
    violation = ModelDependencyViolation(
        kind=EnumDependencyViolationKind.CYCLE,
        ticket_id="OMN-005",
        cycle_path=("OMN-005", "OMN-006", "OMN-005"),
        message="Cycle detected: OMN-005 → OMN-006 → OMN-005",
    )

    assert violation.kind == EnumDependencyViolationKind.CYCLE
    assert len(violation.cycle_path) == 3
    assert violation.dependency_id == ""


@pytest.mark.unit
def test_model_dependency_violation_missing() -> None:
    violation = ModelDependencyViolation(
        kind=EnumDependencyViolationKind.MISSING_DEPENDENCY,
        ticket_id="OMN-007",
        dependency_id="OMN-999",
        message="OMN-007 depends on OMN-999 which is not in the plan",
    )

    assert violation.kind == EnumDependencyViolationKind.MISSING_DEPENDENCY
    assert violation.dependency_id == "OMN-999"
    assert violation.cycle_path == ()


@pytest.mark.unit
def test_enum_wave_scheduler_status_values() -> None:
    assert EnumWaveSchedulerStatus.COMPLETED.value == "completed"
    assert EnumWaveSchedulerStatus.PARTIAL.value == "partial"
    assert EnumWaveSchedulerStatus.FAILED.value == "failed"
    assert EnumWaveSchedulerStatus.DRY_RUN.value == "dry_run"
    assert EnumWaveSchedulerStatus.ABORTED.value == "aborted"


@pytest.mark.unit
def test_enum_ticket_execution_status_values() -> None:
    assert EnumTicketExecutionStatus.COMPLETED.value == "completed"
    assert EnumTicketExecutionStatus.FAILED.value == "failed"
    assert EnumTicketExecutionStatus.BLOCKED.value == "blocked"
    assert EnumTicketExecutionStatus.STALLED.value == "stalled"
    assert EnumTicketExecutionStatus.SKIPPED.value == "skipped"
    assert EnumTicketExecutionStatus.TIMEOUT.value == "timeout"
    assert EnumTicketExecutionStatus.DEFERRED.value == "deferred"


@pytest.mark.unit
def test_enum_dependency_violation_kind_values() -> None:
    assert EnumDependencyViolationKind.CYCLE.value == "cycle"
    assert EnumDependencyViolationKind.MISSING_DEPENDENCY.value == "missing_dependency"
    assert EnumDependencyViolationKind.SELF_REFERENCE.value == "self_reference"


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_wave_scheduler_orchestrator_dry_run_computes_waves(tmp_path: Path) -> None:
    plan = tmp_path / "plan.yaml"
    plan.write_text(
        yaml.safe_dump(
            {
                "tickets": [
                    {"ticket_id": "OMN-1", "repo": "omnimarket"},
                    {
                        "ticket_id": "OMN-2",
                        "repo": "omnibase_core",
                        "depends_on": ["OMN-1"],
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    handler = HandlerWaveSchedulerOrchestrator()
    request = ModelWaveSchedulerRequest(plan_path=str(plan), dry_run=True)

    result = handler.handle(request)

    assert result.run_status is EnumWaveSchedulerStatus.DRY_RUN
    assert [wave.ticket_ids for wave in result.wave_assignments] == [
        ("OMN-1",),
        ("OMN-2",),
    ]


@pytest.mark.unit
def test_wave_scheduler_orchestrator_live_uses_the_default_effect_boundary(
    tmp_path: Path,
) -> None:
    """OMN-17017: a live run no longer raises ``dispatcher adapter required``.

    ``ProtocolWaveDispatcher`` had no implementation anywhere in the repo, and
    the previous version of THIS test asserted that RuntimeError as expected
    behaviour — codifying "the executor does not exist" as a passing guardrail.
    """
    plan = tmp_path / "plan.yaml"
    plan.write_text(
        yaml.safe_dump({"tickets": [{"ticket_id": "OMN-1"}]}),
        encoding="utf-8",
    )
    handler = HandlerWaveSchedulerOrchestrator()
    request = ModelWaveSchedulerRequest(
        plan_path=str(plan), state_dir=str(tmp_path / "state")
    )

    result = handler.handle(request)

    # Nothing acknowledged the dispatch, so the ticket stays visibly pending.
    assert result.run_status is EnumWaveSchedulerStatus.PARTIAL
    assert result.tickets_unreported == 1
    assert result.dispatch_lifecycle_path is not None
