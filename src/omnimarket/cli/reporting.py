"""Adapters from node execution results to shared OmniMarket CLI reports."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from enum import Enum
from importlib import resources
from uuid import UUID, uuid4

import yaml
from pydantic import BaseModel

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


def load_contract_metadata(package_name: str) -> tuple[str, str, str]:
    """Return contract name, semantic version, and terminal event for a node package."""
    contract_path = resources.files(package_name).joinpath("contract.yaml")
    raw = yaml.safe_load(contract_path.read_text(encoding="utf-8")) or {}
    version = raw["contract_version"]
    contract_version = f"{version['major']}.{version['minor']}.{version['patch']}"
    return str(raw["name"]), contract_version, str(raw["terminal_event"])


def _json_ready(value: object) -> object:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, list | tuple | set):
        return [_json_ready(item) for item in value]
    if isinstance(value, Enum):
        return str(value.value)
    return value


def _result_payload(result: BaseModel | Mapping[str, object]) -> dict[str, object]:
    if isinstance(result, BaseModel):
        payload = result.model_dump(mode="json")
    else:
        payload = dict(result)
    return {str(key): _json_ready(value) for key, value in payload.items()}


def _coerce_correlation_id(payload: Mapping[str, object]) -> UUID:
    value = payload.get("correlation_id")
    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        try:
            return UUID(value)
        except ValueError:
            pass
    return uuid4()


def _status_from_payload(payload: Mapping[str, object]) -> EnumMarketCliStatus:
    for field in ("status", "final_state", "final_phase", "current_phase"):
        raw = payload.get(field)
        if raw is None:
            continue
        value = str(raw).lower()
        if value in {"failed", "failure"}:
            return EnumMarketCliStatus.FAILED
        if value == "error":
            return EnumMarketCliStatus.ERROR
        if value in {"blocked"}:
            return EnumMarketCliStatus.BLOCKED
        if value in {"complete", "completed", "ready", "done", "clean", "succeeded"}:
            return EnumMarketCliStatus.SUCCESS
        if value in {"findings", "partial", "init", "running"}:
            return EnumMarketCliStatus.PARTIAL
        if value in {"skipped"}:
            return EnumMarketCliStatus.SKIPPED
    return EnumMarketCliStatus.SUCCESS


def _summarize_payload(payload: Mapping[str, object]) -> dict[str, object]:
    summary: dict[str, object] = {}
    for key in (
        "status",
        "final_state",
        "final_phase",
        "current_phase",
        "halt_reason",
        "error_message",
        "dry_run",
        "repo",
        "pr_number",
        "session_id",
        "total_threads",
        "blocking_count",
        "suggestion_count",
        "unknown_count",
        "repos_scanned",
        "findings_count",
        "max_iterations",
        "required_clean_runs",
    ):
        if key in payload:
            summary[key] = payload[key]
    for key in ("findings", "dispatch_queue", "dispatch_receipts", "crons_registered"):
        value = payload.get(key)
        if isinstance(value, list):
            summary[f"{key}_count"] = len(value)
    if not summary:
        summary["result_fields"] = sorted(payload.keys())
    return summary


def build_report_from_model_result(
    result: BaseModel | Mapping[str, object],
    *,
    skill_name: str,
    node_name: str,
    terminal_event: str,
    contract_name: str,
    contract_version: str,
    mode: str,
    input_summary: dict[str, object],
    output_config: ModelMarketCliOutputConfig,
    result_summary: dict[str, object] | None = None,
    steps: list[ModelMarketCliStep] | None = None,
) -> ModelMarketCliReport:
    """Build a shared CLI report from a node result model."""
    payload = _result_payload(result)
    now = datetime.now(UTC)
    started_at = payload.get("started_at")
    completed_at = payload.get("completed_at")
    resolved_started_at = (
        datetime.fromisoformat(started_at) if isinstance(started_at, str) else now
    )
    resolved_completed_at = (
        datetime.fromisoformat(completed_at) if isinstance(completed_at, str) else now
    )
    duration = resolved_completed_at - resolved_started_at
    duration_ms = max(0, int(duration.total_seconds() * 1000))
    return ModelMarketCliReport(
        skill_name=skill_name,
        node_name=node_name,
        contract_name=contract_name,
        contract_version=contract_version,
        run_id=uuid4(),
        correlation_id=_coerce_correlation_id(payload),
        mode=mode,
        status=_status_from_payload(payload),
        input_summary=ModelMarketCliInputSummary(fields=input_summary),
        steps=steps or [],
        evidence=[],
        result_summary=result_summary or _summarize_payload(payload),
        terminal_event=terminal_event,
        output_config=output_config,
        started_at=resolved_started_at,
        completed_at=resolved_completed_at,
        duration_ms=duration_ms,
    )


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
    result_status = str(result.status).lower()
    if result_status in {"error"}:
        status = EnumMarketCliStatus.ERROR
    elif result_status in {"failed", "failure"}:
        status = EnumMarketCliStatus.FAILED
    elif result_status in {"warning", "partial"}:
        status = EnumMarketCliStatus.PARTIAL
    elif result_status in {"blocked"}:
        status = EnumMarketCliStatus.BLOCKED
    else:
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
    "build_report_from_model_result",
    "build_report_from_pipeline_result",
    "load_contract_metadata",
]
