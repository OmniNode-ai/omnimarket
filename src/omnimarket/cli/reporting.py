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
from omnimarket.nodes.node_merge_sweep_compute.handlers.handler_merge_sweep import (
    EnumPRTrack,
    ModelMergeSweepResult,
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


def build_report_from_merge_sweep_result(
    result: ModelMergeSweepResult,
    *,
    skill_name: str,
    node_name: str,
    terminal_event: str,
    contract_name: str,
    contract_version: str,
    mode: str,
    input_summary: dict[str, object],
    output_config: ModelMarketCliOutputConfig,
) -> ModelMarketCliReport:
    """Build the shared CLI report from a merge-sweep classification result."""
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    by_track = {
        track.value: sum(1 for item in result.classified if item.track == track)
        for track in EnumPRTrack
    }
    actionable_count = sum(
        by_track[track.value]
        for track in (
            EnumPRTrack.A_UPDATE,
            EnumPRTrack.A_MERGE,
            EnumPRTrack.A_RESOLVE,
            EnumPRTrack.B_POLISH,
        )
    )
    status = EnumMarketCliStatus.SUCCESS

    return ModelMarketCliReport(
        skill_name=skill_name,
        node_name=node_name,
        contract_name=contract_name,
        contract_version=contract_version,
        run_id=uuid4(),
        correlation_id=uuid4(),
        mode=mode,
        status=status,
        input_summary=ModelMarketCliInputSummary(fields=input_summary),
        steps=[
            ModelMarketCliStep(
                name=f"{item.pr.repo}#{item.pr.number}",
                status=item.track.value,
                description=item.reason,
                details={
                    "title": item.pr.title,
                    "mergeable": item.pr.mergeable,
                    "merge_state_status": item.pr.merge_state_status,
                    "review_decision": item.pr.review_decision,
                    "required_checks_pass": item.pr.required_checks_pass,
                    "failure_categories": list(item.failure_categories),
                },
            )
            for item in result.classified
        ],
        evidence=[],
        result_summary={
            "merge_sweep_status": result.status,
            "classified_count": len(result.classified),
            "actionable_count": actionable_count,
            "track_counts": by_track,
            "failure_history": result.failure_history_summary.model_dump(),
        },
        terminal_event=terminal_event,
        output_config=output_config,
        started_at=now,
        completed_at=now,
        duration_ms=0,
    )


__all__ = [
    "build_report_from_merge_sweep_result",
    "build_report_from_pipeline_result",
]
