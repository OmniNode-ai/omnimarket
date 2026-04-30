from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
import yaml

from omnimarket.cli.reporting import build_report_from_pipeline_result
from omnimarket.models.cli_report import EnumMarketCliStatus
from omnimarket.nodes.node_ticket_pipeline.models.model_pipeline_completed_event import (
    ModelPipelineCompletedEvent,
)
from omnimarket.nodes.node_ticket_pipeline.models.model_pipeline_phase_event import (
    ModelPipelinePhaseEvent,
)
from omnimarket.nodes.node_ticket_pipeline.models.model_pipeline_phase_result import (
    EnumPipelinePhaseResultStatus,
    ModelPipelineExecutionReport,
    ModelPipelinePhaseResult,
)
from omnimarket.nodes.node_ticket_pipeline.models.model_pipeline_state import (
    EnumPipelinePhase,
    ModelPipelineState,
)


def _ticket_pipeline_terminal_event() -> str:
    contract_path = (
        Path(__file__).resolve().parents[3]
        / "src"
        / "omnimarket"
        / "nodes"
        / "node_ticket_pipeline"
        / "contract.yaml"
    )
    raw = yaml.safe_load(contract_path.read_text())
    return str(raw["terminal_event"])


@pytest.fixture
def sample_pipeline_report() -> ModelPipelineExecutionReport:
    correlation_id = UUID("22222222-2222-4222-8222-222222222222")
    started_at = datetime(2026, 4, 30, 14, 0, 0, tzinfo=UTC)
    blocked_at = datetime(2026, 4, 30, 14, 0, 1, tzinfo=UTC)
    state = ModelPipelineState(
        correlation_id=correlation_id,
        ticket_id="OMN-9530",
        current_phase=EnumPipelinePhase.BLOCKED,
        dry_run=True,
        error_message="Phase local_review is not implemented",
    )
    phase_results = [
        ModelPipelinePhaseResult(
            correlation_id=correlation_id,
            ticket_id="OMN-9530",
            phase=EnumPipelinePhase.PRE_FLIGHT,
            status=EnumPipelinePhaseResultStatus.SUCCEEDED,
            dry_run=True,
            started_at=started_at,
            completed_at=started_at,
            message="Pre-flight validation passed",
            details={"side_effects": "none"},
        ),
        ModelPipelinePhaseResult(
            correlation_id=correlation_id,
            ticket_id="OMN-9530",
            phase=EnumPipelinePhase.LOCAL_REVIEW,
            status=EnumPipelinePhaseResultStatus.NOT_IMPLEMENTED,
            dry_run=True,
            started_at=blocked_at,
            completed_at=blocked_at,
            message="Phase local_review is not implemented",
            details={"blocked_reason": "phase_not_wired"},
        ),
    ]
    return ModelPipelineExecutionReport(
        state=state,
        phase_results=phase_results,
        phase_events=[
            ModelPipelinePhaseEvent(
                correlation_id=correlation_id,
                ticket_id="OMN-9530",
                from_phase=EnumPipelinePhase.LOCAL_REVIEW,
                to_phase=EnumPipelinePhase.BLOCKED,
                success=False,
                timestamp=blocked_at,
                error_message="Phase local_review is not implemented",
            )
        ],
        completed=ModelPipelineCompletedEvent(
            correlation_id=correlation_id,
            ticket_id="OMN-9530",
            final_phase=EnumPipelinePhase.BLOCKED,
            started_at=started_at,
            completed_at=blocked_at,
            error_message="Phase local_review is not implemented",
        ),
        ran_phase=EnumPipelinePhase.LOCAL_REVIEW,
        stopped_at=EnumPipelinePhase.BLOCKED,
        stop_reason=EnumPipelinePhaseResultStatus.NOT_IMPLEMENTED.value,
    )


def test_pipeline_report_maps_to_market_cli_report(
    sample_pipeline_report: ModelPipelineExecutionReport,
) -> None:
    out = build_report_from_pipeline_result(
        sample_pipeline_report,
        skill_name="ticket_pipeline",
        node_name="node_ticket_pipeline",
        terminal_event=_ticket_pipeline_terminal_event(),
        contract_name="ticket_pipeline",
        contract_version="1.0.0",
        mode="dry_run",
        input_summary={"ticket_id": "OMN-9530"},
    )

    assert out.skill_name == "ticket_pipeline"
    assert out.status == EnumMarketCliStatus.BLOCKED
    assert out.result_summary["stop_reason"] == "not_implemented"
    assert out.terminal_event == _ticket_pipeline_terminal_event()
    assert any(
        step.status == EnumPipelinePhaseResultStatus.SUCCEEDED.value
        for step in out.steps
    )
