"""Adapters from node execution results to shared OmniMarket CLI reports."""

from __future__ import annotations

from uuid import uuid4

from omnimarket.models.cli_report import (
    EnumMarketCliOutputFormat,
    EnumMarketCliStatus,
    EnumMarketCliVerbosity,
    ModelMarketCliInputSummary,
    ModelMarketCliOutputConfig,
    ModelMarketCliReport,
    ModelMarketCliStep,
)
from omnimarket.nodes.node_ticket_pipeline.models.model_pipeline_phase_result import (
    ModelPipelineExecutionReport,
)
from omnimarket.nodes.node_ticket_pipeline.models.model_pipeline_state import (
    EnumPipelinePhase,
)


def _status_from_pipeline_result(
    report: ModelPipelineExecutionReport,
) -> EnumMarketCliStatus:
    if report.stop_reason == "not_implemented":
        return EnumMarketCliStatus.BLOCKED
    if report.stop_reason == "failed":
        return EnumMarketCliStatus.FAILED
    if report.stop_reason == "succeeded":
        return EnumMarketCliStatus.SUCCESS
    if report.stopped_at == EnumPipelinePhase.DONE:
        return EnumMarketCliStatus.SUCCESS
    if report.stopped_at == EnumPipelinePhase.FAILED:
        return EnumMarketCliStatus.FAILED
    if report.stopped_at == EnumPipelinePhase.BLOCKED:
        return EnumMarketCliStatus.BLOCKED
    if report.phase_results:
        return EnumMarketCliStatus.PARTIAL
    return EnumMarketCliStatus.SKIPPED


def build_report_from_pipeline_result(
    report: ModelPipelineExecutionReport,
    *,
    skill_name: str,
    node_name: str,
    terminal_event: str,
    contract_name: str,
    contract_version: str,
    mode: str,
    input_summary: dict[str, object],
    output_config: ModelMarketCliOutputConfig | None = None,
) -> ModelMarketCliReport:
    """Build the shared CLI report from a ticket-pipeline execution result."""
    completed = report.completed
    duration = completed.completed_at - completed.started_at
    duration_ms = max(0, int(duration.total_seconds() * 1000))
    resolved_output_config = output_config or ModelMarketCliOutputConfig(
        format=EnumMarketCliOutputFormat.JSON,
        verbosity=EnumMarketCliVerbosity.STANDARD,
    )

    return ModelMarketCliReport(
        skill_name=skill_name,
        node_name=node_name,
        contract_name=contract_name,
        contract_version=contract_version,
        run_id=uuid4(),
        correlation_id=completed.correlation_id,
        mode=mode,
        status=_status_from_pipeline_result(report),
        input_summary=ModelMarketCliInputSummary(fields=input_summary),
        steps=[
            ModelMarketCliStep(
                name=result.phase.value,
                status=result.status.value,
                description=result.message or "",
                details=result.details,
            )
            for result in report.phase_results
        ],
        evidence=[],
        result_summary={
            "stopped_at": report.stopped_at.value,
            "stop_reason": report.stop_reason,
            "ran_phase": report.ran_phase.value if report.ran_phase else None,
            "final_phase": completed.final_phase.value,
            "error_message": completed.error_message,
            "phase_results_count": len(report.phase_results),
        },
        terminal_event=terminal_event,
        output_config=resolved_output_config,
        started_at=completed.started_at,
        completed_at=completed.completed_at,
        duration_ms=duration_ms,
    )


__all__: list[str] = ["build_report_from_pipeline_result"]
