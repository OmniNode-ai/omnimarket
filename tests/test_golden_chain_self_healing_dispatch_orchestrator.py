# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden-chain guardrails for node_self_healing_dispatch_orchestrator [OMN-12208].

Honest routing behaviour for an explicit stub node:
- contract marks node_not_implemented: true
- entry point loads
- typed models are strict (frozen, extra="forbid")
- handler fails loudly with NotImplementedError containing "node_not_implemented"
- contract declares the expected runtime routing surface
"""

from __future__ import annotations

from importlib.metadata import entry_points
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_self_healing_dispatch_orchestrator.handlers.handler_self_healing_dispatch_orchestrator import (
    HandlerSelfHealingDispatchOrchestrator,
)
from omnimarket.nodes.node_self_healing_dispatch_orchestrator.models.model_self_healing_dispatch_request import (
    ModelSelfHealingDispatchRequest,
)
from omnimarket.nodes.node_self_healing_dispatch_orchestrator.models.model_self_healing_dispatch_result import (
    EnumDispatchRunStatus,
    EnumWorkerStatus,
    ModelDispatchGroup,
    ModelEscalationRecord,
    ModelSelfHealingDispatchResult,
    ModelStallRecoveryEvent,
    ModelWorkerRecord,
)

_NODE_NAME = "node_self_healing_dispatch_orchestrator"
_HANDLER_MODULE = (
    "omnimarket.nodes.node_self_healing_dispatch_orchestrator"
    ".handlers.handler_self_healing_dispatch_orchestrator"
)
_HANDLER_CLASS = "HandlerSelfHealingDispatchOrchestrator"
_REQUEST_MODULE = (
    "omnimarket.nodes.node_self_healing_dispatch_orchestrator"
    ".models.model_self_healing_dispatch_request"
)
_REQUEST_CLASS = "ModelSelfHealingDispatchRequest"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _contract() -> dict:  # type: ignore[type-arg]
    path = _repo_root() / "src" / "omnimarket" / "nodes" / _NODE_NAME / "contract.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Contract shape
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_self_healing_dispatch_orchestrator_contract_is_explicit_stub() -> None:
    raw = _contract()

    assert raw["node_not_implemented"] is True
    assert raw["node_type"] == "orchestrator"
    assert raw["handler"]["module"] == _HANDLER_MODULE
    assert raw["handler"]["class"] == _HANDLER_CLASS
    assert raw["handler"]["input_model"] == f"{_REQUEST_MODULE}.{_REQUEST_CLASS}"


@pytest.mark.unit
def test_self_healing_dispatch_orchestrator_contract_routing_surface() -> None:
    raw = _contract()

    assert raw["handler_routing"]["routing_strategy"] == "operation_match"
    assert raw["handler_routing"]["handlers"] == [
        {
            "handler": {
                "name": _HANDLER_CLASS,
                "module": _HANDLER_MODULE,
            }
        }
    ]


@pytest.mark.unit
def test_self_healing_dispatch_orchestrator_contract_event_bus() -> None:
    raw = _contract()
    eb = raw["event_bus"]

    assert (
        eb["consumer_group"]
        == "omnimarket.self_healing_dispatch_orchestrator.consume.v1"
    )
    assert (
        "onex.cmd.omnimarket.self-healing-dispatch-start.v1" in eb["subscribe_topics"]
    )
    assert (
        "onex.evt.omnimarket.self-healing-dispatch-completed.v1" in eb["publish_topics"]
    )
    assert (
        "onex.evt.omnimarket.self-healing-dispatch-planned.v1" in eb["publish_topics"]
    )
    assert (
        "onex.evt.omnimarket.self-healing-dispatch-stall-detected.v1"
        in eb["publish_topics"]
    )
    assert (
        "onex.evt.omnimarket.self-healing-dispatch-escalated.v1" in eb["publish_topics"]
    )
    assert "onex.dlq.omnimarket.self-healing-dispatch.v1" in eb["dlq_topics"]


@pytest.mark.unit
def test_self_healing_dispatch_orchestrator_terminal_event() -> None:
    raw = _contract()
    assert (
        raw["terminal_event"]
        == "onex.evt.omnimarket.self-healing-dispatch-completed.v1"
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_self_healing_dispatch_orchestrator_entry_point_loads() -> None:
    eps = {ep.name: ep for ep in entry_points(group="onex.nodes")}

    loaded = eps[_NODE_NAME].load()

    assert loaded.__name__ == f"omnimarket.nodes.{_NODE_NAME}"


# ---------------------------------------------------------------------------
# Input model (ModelSelfHealingDispatchRequest)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_model_self_healing_dispatch_request_minimal_tickets() -> None:
    req = ModelSelfHealingDispatchRequest(ticket_ids=("OMN-1234", "OMN-5678"))

    assert req.ticket_ids == ("OMN-1234", "OMN-5678")
    assert req.epic_id == ""
    assert req.repo_hints == {}
    assert req.run_id == ""
    assert req.max_redispatches == 2
    assert req.healthcheck_interval_seconds == 120
    assert req.dry_run is False


@pytest.mark.unit
def test_model_self_healing_dispatch_request_minimal_epic() -> None:
    req = ModelSelfHealingDispatchRequest(epic_id="OMN-7253")

    assert req.epic_id == "OMN-7253"
    assert req.ticket_ids == ()


@pytest.mark.unit
def test_model_self_healing_dispatch_request_with_repo_hints() -> None:
    req = ModelSelfHealingDispatchRequest(
        ticket_ids=("OMN-1234",),
        repo_hints={"OMN-1234": "omniclaude"},
    )

    assert req.repo_hints == {"OMN-1234": "omniclaude"}


@pytest.mark.unit
def test_model_self_healing_dispatch_request_all_fields() -> None:
    req = ModelSelfHealingDispatchRequest(
        ticket_ids=("OMN-1234", "OMN-5678"),
        repo_hints={"OMN-1234": "omniclaude"},
        run_id="orch-20260525T120000Z",
        max_redispatches=3,
        healthcheck_interval_seconds=60,
        dry_run=True,
    )

    assert req.run_id == "orch-20260525T120000Z"
    assert req.max_redispatches == 3
    assert req.healthcheck_interval_seconds == 60
    assert req.dry_run is True


@pytest.mark.unit
def test_model_self_healing_dispatch_request_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        ModelSelfHealingDispatchRequest(
            ticket_ids=("OMN-1234",),
            unexpected_field=True,  # type: ignore[call-arg]
        )


@pytest.mark.unit
def test_model_self_healing_dispatch_request_is_frozen() -> None:
    req = ModelSelfHealingDispatchRequest(ticket_ids=("OMN-1234",))

    with pytest.raises(ValidationError):
        req.dry_run = True  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Output model (ModelSelfHealingDispatchResult)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_model_self_healing_dispatch_result_minimal() -> None:
    result = ModelSelfHealingDispatchResult(
        run_id="orch-20260525T120000Z",
        run_status=EnumDispatchRunStatus.COMPLETED,
    )

    assert result.run_id == "orch-20260525T120000Z"
    assert result.run_status == EnumDispatchRunStatus.COMPLETED
    assert result.dispatch_groups == ()
    assert result.dispatched_workers == ()
    assert result.stall_events == ()
    assert result.escalated_tickets == ()
    assert result.total_tickets == 0
    assert result.stalls_recovered == 0
    assert result.elapsed_seconds == 0
    assert result.dry_run is False
    assert result.log_path == ""


@pytest.mark.unit
def test_model_self_healing_dispatch_result_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        ModelSelfHealingDispatchResult(
            run_id="orch-test",
            run_status=EnumDispatchRunStatus.COMPLETED,
            bogus_field=True,  # type: ignore[call-arg]
        )


@pytest.mark.unit
def test_model_dispatch_group_fields() -> None:
    group = ModelDispatchGroup(
        repo="omniclaude",
        ticket_ids=("OMN-1234", "OMN-5678"),
        worker_name="worker-omniclaude-orch-test",
    )

    assert group.repo == "omniclaude"
    assert group.ticket_ids == ("OMN-1234", "OMN-5678")
    assert group.worker_name == "worker-omniclaude-orch-test"


@pytest.mark.unit
def test_model_worker_record_fields() -> None:
    record = ModelWorkerRecord(
        worker_name="worker-omniclaude-orch-test",
        repo="omniclaude",
        ticket_ids=("OMN-1234",),
        status=EnumWorkerStatus.COMPLETED,
    )

    assert record.worker_name == "worker-omniclaude-orch-test"
    assert record.status == EnumWorkerStatus.COMPLETED
    assert record.redispatch_attempt == 0


@pytest.mark.unit
def test_model_stall_recovery_event_fields() -> None:
    event = ModelStallRecoveryEvent(
        ticket_id="OMN-1234",
        repo="omniclaude",
        redispatch_attempt=1,
        max_redispatches=2,
        recovery_worker_name="recovery-OMN-1234-attempt-1",
        escalated=False,
    )

    assert event.ticket_id == "OMN-1234"
    assert event.redispatch_attempt == 1
    assert event.escalated is False


@pytest.mark.unit
def test_model_escalation_record_fields() -> None:
    record = ModelEscalationRecord(
        ticket_id="OMN-9999",
        repo="omnibase_core",
        attempt_count=3,
    )

    assert record.ticket_id == "OMN-9999"
    assert record.attempt_count == 3


@pytest.mark.unit
def test_enum_dispatch_run_status_values() -> None:
    assert EnumDispatchRunStatus.COMPLETED == "completed"
    assert EnumDispatchRunStatus.PARTIAL == "partial"
    assert EnumDispatchRunStatus.FAILED == "failed"
    assert EnumDispatchRunStatus.DRY_RUN == "dry_run"


@pytest.mark.unit
def test_enum_worker_status_values() -> None:
    assert EnumWorkerStatus.COMPLETED == "completed"
    assert EnumWorkerStatus.STALLED == "stalled"
    assert EnumWorkerStatus.ESCALATED == "escalated"
    assert EnumWorkerStatus.FAILED == "failed"


# ---------------------------------------------------------------------------
# Handler stub (fails loudly)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_self_healing_dispatch_orchestrator_handler_fails_loudly() -> None:
    handler = HandlerSelfHealingDispatchOrchestrator()
    request = ModelSelfHealingDispatchRequest(ticket_ids=("OMN-1234",))

    with pytest.raises(NotImplementedError, match="node_not_implemented"):
        handler.handle(request)
