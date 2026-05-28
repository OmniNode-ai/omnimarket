# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden-chain guardrails for node_epic_team_orchestrator [OMN-12206]."""

from __future__ import annotations

from importlib.metadata import entry_points
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_epic_team_orchestrator.handlers.handler_epic_team_orchestrator import (
    HandlerEpicTeamOrchestrator,
)
from omnimarket.nodes.node_epic_team_orchestrator.models.model_epic_team_request import (
    EnumEpicTeamMode,
    ModelEpicTeamRequest,
)
from omnimarket.nodes.node_epic_team_orchestrator.models.model_epic_team_result import (
    EnumDodGateStatus,
    EnumEpicTeamRunStatus,
    EnumTicketDisposition,
    ModelEpicTeamResult,
    ModelStallEvent,
    ModelTicketOutcome,
    ModelWaveResult,
)

_NODE_NAME = "node_epic_team_orchestrator"
_HANDLER_MODULE = "omnimarket.nodes.node_epic_team_orchestrator.handlers.handler_epic_team_orchestrator"
_HANDLER_CLASS = "HandlerEpicTeamOrchestrator"
_REQUEST_MODULE = (
    "omnimarket.nodes.node_epic_team_orchestrator.models.model_epic_team_request"
)
_REQUEST_CLASS = "ModelEpicTeamRequest"


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
def test_epic_team_orchestrator_contract_is_implemented() -> None:
    raw = _contract()

    assert raw["node_not_implemented"] is False
    assert raw["node_type"] == "orchestrator"
    assert raw["handler"]["module"] == _HANDLER_MODULE
    assert raw["handler"]["class"] == _HANDLER_CLASS
    assert raw["handler"]["input_model"] == f"{_REQUEST_MODULE}.{_REQUEST_CLASS}"


@pytest.mark.unit
def test_epic_team_orchestrator_contract_routing_surface() -> None:
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
def test_epic_team_orchestrator_contract_event_bus() -> None:
    raw = _contract()
    eb = raw["event_bus"]

    assert eb["consumer_group"] == "omnimarket.epic_team_orchestrator.consume.v1"
    assert "onex.cmd.omnimarket.epic-team-start.v1" in eb["subscribe_topics"]
    assert "onex.evt.omnimarket.epic-team-completed.v1" in eb["publish_topics"]
    assert "onex.evt.omnimarket.epic-team-wave-dispatched.v1" in eb["publish_topics"]
    assert "onex.evt.omnimarket.epic-team-stall-detected.v1" in eb["publish_topics"]
    assert "onex.dlq.omnimarket.epic-team.v1" in eb["dlq_topics"]


@pytest.mark.unit
def test_epic_team_orchestrator_terminal_event() -> None:
    raw = _contract()
    assert raw["terminal_event"] == "onex.evt.omnimarket.epic-team-completed.v1"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_epic_team_orchestrator_entry_point_loads() -> None:
    eps = {ep.name: ep for ep in entry_points(group="onex.nodes")}

    loaded = eps[_NODE_NAME].load()

    assert loaded.__name__ == f"omnimarket.nodes.{_NODE_NAME}"


# ---------------------------------------------------------------------------
# Input model (ModelEpicTeamRequest)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_model_epic_team_request_minimal() -> None:
    req = ModelEpicTeamRequest(epic_id="OMN-9999")

    assert req.epic_id == "OMN-9999"
    assert req.mode == EnumEpicTeamMode.BUILD
    assert req.dry_run is False
    assert req.force is False
    assert req.force_kill is False
    assert req.resume is False
    assert req.force_unmatched is False


@pytest.mark.unit
def test_model_epic_team_request_all_flags() -> None:
    req = ModelEpicTeamRequest(
        epic_id="OMN-1234",
        mode=EnumEpicTeamMode.BUILD,
        dry_run=True,
        force=True,
        force_kill=True,
        resume=False,
        force_unmatched=True,
    )

    assert req.epic_id == "OMN-1234"
    assert req.dry_run is True
    assert req.force is True
    assert req.force_kill is True
    assert req.force_unmatched is True


@pytest.mark.unit
def test_model_epic_team_request_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        ModelEpicTeamRequest(epic_id="OMN-9999", unexpected_field=True)  # type: ignore[call-arg]


@pytest.mark.unit
def test_model_epic_team_request_is_frozen() -> None:
    req = ModelEpicTeamRequest(epic_id="OMN-9999")

    with pytest.raises(ValidationError):
        req.epic_id = "OMN-0000"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Output model (ModelEpicTeamResult)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_model_epic_team_result_minimal() -> None:
    result = ModelEpicTeamResult(
        epic_id="OMN-9999",
        run_status=EnumEpicTeamRunStatus.COMPLETED,
    )

    assert result.epic_id == "OMN-9999"
    assert result.run_status == EnumEpicTeamRunStatus.COMPLETED
    assert result.wave_results == ()
    assert result.completed_tickets == ()
    assert result.failed_tickets == ()
    assert result.stall_events == ()
    assert result.dod_gate_status == EnumDodGateStatus.SKIPPED
    assert result.total_tickets == 0
    assert result.dry_run is False


@pytest.mark.unit
def test_model_epic_team_result_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        ModelEpicTeamResult(
            epic_id="OMN-9999",
            run_status=EnumEpicTeamRunStatus.COMPLETED,
            bogus_field=True,  # type: ignore[call-arg]
        )


@pytest.mark.unit
def test_model_wave_result_fields() -> None:
    wave = ModelWaveResult(
        wave_id=0,
        dispatched_count=5,
        merged_count=4,
        failed_count=1,
        stalled_count=0,
        skipped_count=0,
    )

    assert wave.wave_id == 0
    assert wave.dispatched_count == 5
    assert wave.merged_count == 4
    assert wave.failed_count == 1


@pytest.mark.unit
def test_model_ticket_outcome_fields() -> None:
    outcome = ModelTicketOutcome(
        ticket_id="OMN-2001",
        repo="omniclaude",
        disposition=EnumTicketDisposition.MERGED,
        pr_url="https://github.com/OmniNode-ai/omniclaude/pull/99",
        branch="jonah/omn-2001-feature",
        wave_id=0,
    )

    assert outcome.ticket_id == "OMN-2001"
    assert outcome.disposition == EnumTicketDisposition.MERGED
    assert outcome.wave_id == 0
    assert outcome.retry_count == 0


@pytest.mark.unit
def test_model_stall_event_fields() -> None:
    stall = ModelStallEvent(
        ticket_id="OMN-2002",
        wave_id=1,
        idle_seconds=420,
        retry_wave=2,
    )

    assert stall.ticket_id == "OMN-2002"
    assert stall.idle_seconds == 420
    assert stall.retry_wave == 2


@pytest.mark.unit
def test_enum_ticket_disposition_values() -> None:
    assert EnumTicketDisposition.MERGED.value == "merged"
    assert EnumTicketDisposition.FAILED.value == "failed"
    assert EnumTicketDisposition.BLOCKED.value == "blocked"
    assert EnumTicketDisposition.STALLED.value == "stalled"
    assert EnumTicketDisposition.SKIPPED.value == "skipped"
    assert EnumTicketDisposition.TIMEOUT.value == "timeout"


@pytest.mark.unit
def test_enum_epic_team_run_status_values() -> None:
    assert EnumEpicTeamRunStatus.COMPLETED.value == "completed"
    assert EnumEpicTeamRunStatus.PARTIAL.value == "partial"
    assert EnumEpicTeamRunStatus.FAILED.value == "failed"
    assert EnumEpicTeamRunStatus.DRY_RUN.value == "dry_run"


@pytest.mark.unit
def test_enum_dod_gate_status_values() -> None:
    assert EnumDodGateStatus.PASS.value == "pass"
    assert EnumDodGateStatus.FAIL.value == "fail"
    assert EnumDodGateStatus.UNKNOWN.value == "unknown"
    assert EnumDodGateStatus.SKIPPED.value == "skipped"


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_epic_team_orchestrator_dry_run_returns_empty_plan() -> None:
    handler = HandlerEpicTeamOrchestrator()
    request = ModelEpicTeamRequest(epic_id="OMN-9999", dry_run=True)

    result = handler.handle(request)

    assert result.run_status is EnumEpicTeamRunStatus.DRY_RUN
    assert result.epic_id == "OMN-9999"
    assert result.total_tickets == 0


@pytest.mark.unit
def test_epic_team_orchestrator_live_requires_executor() -> None:
    handler = HandlerEpicTeamOrchestrator()
    request = ModelEpicTeamRequest(epic_id="OMN-9999")

    with pytest.raises(RuntimeError, match="executor adapter required"):
        handler.handle(request)
