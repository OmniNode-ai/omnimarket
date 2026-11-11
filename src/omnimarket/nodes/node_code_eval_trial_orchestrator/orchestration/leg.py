# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One leg of the code eval trial orchestrator: a message to the next row and its emissions (OMN-23627).

Exemplar: ``nodes/node_pr_landing_orchestrator/orchestration/core.py`` (``run_leg``:
the stored row plus one message through the pure reducer, then the typed emissions).
The reducer decides (``node_code_eval_trial_reducer``); this module composes: it
reads the run leg's inputs through its ports, calls the reducer in process, and
turns the reducer's emissions into the typed commands, rows and events the
contract publishes. It waits for nothing.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel

from omnimarket.lab_work.bus import ModelLabWorkCommandFailure
from omnimarket.nodes.node_code_contract_score_effect.models.model_code_score import (
    ModelCodeScored,
    ModelCodeScoreOverlay,
)
from omnimarket.nodes.node_code_eval_trial_orchestrator.models.model_code_eval_overlay import (
    ModelCodeEvalRunnerOverlay,
)
from omnimarket.nodes.node_code_eval_trial_orchestrator.models.model_code_eval_run_nudge import (
    ModelCodeEvalRunNudge,
)
from omnimarket.nodes.node_code_eval_trial_orchestrator.models.model_code_eval_run_request import (
    ModelCodeEvalCandidate,
    ModelCodeEvalRunRequest,
)
from omnimarket.nodes.node_code_eval_trial_orchestrator.models.model_code_eval_run_row import (
    ModelCodeEvalRunRow,
    ModelCodeEvalSettings,
)
from omnimarket.nodes.node_code_eval_trial_orchestrator.models.model_code_eval_task_material import (
    ModelCodeEvalTaskMaterial,
    task_key_for,
)
from omnimarket.nodes.node_code_eval_trial_orchestrator.orchestration.builders import (
    build_result_row,
    build_score_request,
    build_unit_request,
)
from omnimarket.nodes.node_code_eval_trial_orchestrator.orchestration.ports import (
    ProtocolCodeEvalFreeHosts,
    ProtocolCodeEvalSuiteReader,
)
from omnimarket.nodes.node_code_eval_trial_reducer.handlers import (
    HandlerCodeEvalTrialReducer,
)
from omnimarket.nodes.node_code_eval_trial_reducer.models import (
    ModelCodeEvalMessage,
    ModelCodeEvalReduceInput,
    ModelNudgeMessage,
    ModelResolvedTask,
    ModelRunRequestedMessage,
    ModelScoreAnsweredMessage,
    ModelUnitAnsweredMessage,
    ModelUnitFailedMessage,
)
from omnimarket.nodes.node_lab_work_unit_effect.models.model_lab_work_unit import (
    ModelLabWorkUnitReceipt,
)


@dataclass(frozen=True)
class CodeEvalLegConfig:
    """Every value a leg reads, from the overlays and the contract (no default)."""

    overlay: ModelCodeEvalRunnerOverlay
    score_overlay: ModelCodeScoreOverlay
    completion_bound_seconds: int
    score_timeout_seconds: int


@dataclass(frozen=True)
class CodeEvalLegPorts:
    suite_reader: ProtocolCodeEvalSuiteReader
    free_hosts: ProtocolCodeEvalFreeHosts
    reducer: HandlerCodeEvalTrialReducer = field(
        default_factory=HandlerCodeEvalTrialReducer
    )


@dataclass(frozen=True)
class CodeEvalLegResult:
    row: ModelCodeEvalRunRow | None
    row_changed: bool
    emitted: tuple[BaseModel, ...]
    transition: str
    dropped: bool


def settings_for(config: CodeEvalLegConfig) -> ModelCodeEvalSettings:
    overlay = config.overlay
    return ModelCodeEvalSettings(
        attempt_budgets=overlay.attempt_budgets,
        try_timeout_seconds=overlay.try_timeout_seconds,
        score_timeout_seconds=config.score_timeout_seconds,
        receipt_slack_seconds=overlay.receipt_slack_seconds,
        nudge_interval_seconds=overlay.nudge_interval_seconds,
        completion_bound_seconds=config.completion_bound_seconds,
    )


def suite_digest(materials: tuple[ModelCodeEvalTaskMaterial, ...]) -> str:
    """The digest every row carries: each task, its merge commit and its material hash (rule 4)."""
    lines = sorted(
        f"{m.task_key}|{m.base_commit}|{m.material_hash}|{m.excluded_reason or ''}"
        for m in materials
    )
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def _candidate_of(
    row: ModelCodeEvalRunRow, task_key: str
) -> ModelCodeEvalCandidate | None:
    for candidate in row.request.candidates:
        if task_key_for(candidate) == task_key:
            return candidate
    return None


def to_message(
    request: BaseModel,
    row: ModelCodeEvalRunRow | None,
    config: CodeEvalLegConfig,
    ports: CodeEvalLegPorts,
) -> ModelCodeEvalMessage | None:
    """The reducer's message for one consumed event, or None when it carries none (capacity, tick)."""
    if isinstance(request, ModelCodeEvalRunRequest):
        materials = ports.suite_reader.read(request.candidates)
        return ModelRunRequestedMessage(
            request=request,
            tasks=tuple(
                ModelResolvedTask(
                    task_key=m.task_key,
                    band=m.band,
                    excluded_reason=m.excluded_reason,
                )
                for m in materials
            ),
            settings=settings_for(config),
            digest=suite_digest(materials),
        )
    if isinstance(request, ModelLabWorkUnitReceipt):
        result = request.code_edit_result
        return ModelUnitAnsweredMessage(
            unit_id=request.work_unit_id,
            status=request.status.value,
            exit_code=request.exit_code,
            diff=request.diff,
            diff_truncated=request.diff_truncated,
            loop_status=result.status.value if result is not None else None,
            duration_s=request.duration_seconds,
        )
    if isinstance(request, ModelLabWorkCommandFailure):
        return ModelUnitFailedMessage(unit_id=request.work_unit_id)
    if isinstance(request, ModelCodeScored):
        verdict = request.verdict
        return ModelScoreAnsweredMessage(
            task_key=request.try_identity.task_key,
            model_arm=request.try_identity.model_arm,
            try_ordinal=request.try_identity.try_ordinal,
            verdict=verdict.checks,
            source=verdict.source,
        )
    if isinstance(request, ModelCodeEvalRunNudge):
        return ModelNudgeMessage(nudge=request)
    return None


def run_leg(
    row: ModelCodeEvalRunRow | None,
    request: BaseModel,
    *,
    now: datetime,
    config: CodeEvalLegConfig,
    ports: CodeEvalLegPorts,
) -> CodeEvalLegResult:
    """Apply one message to the run row; return the next row and what to publish."""
    if isinstance(request, ModelCodeEvalRunRequest) and row is not None:
        # A redelivered run command: the row exists, so nothing is read and nothing changes.
        return CodeEvalLegResult(row, False, (), "dropped:run_already_started", True)
    message = to_message(request, row, config, ports)
    if message is None:
        return CodeEvalLegResult(row, False, (), "dropped:no_run_message", True)
    free = ports.free_hosts.free_hosts(now, config.overlay.need_tools)
    out = ports.reducer.handle(
        ModelCodeEvalReduceInput(row=row, message=message, now=now, free_hosts=free)
    )
    emitted: list[BaseModel] = []
    next_row = out.row
    if next_row is not None:
        run_id = next_row.run_id
        for send in out.score_requests:
            emitted.append(
                build_score_request(
                    run_id,
                    send,
                    _material(next_row, send.task_key, ports),
                    config.score_overlay,
                )
            )
        for place in out.unit_requests:
            emitted.append(
                build_unit_request(
                    run_id,
                    place,
                    _material(next_row, place.task_key, ports),
                    config.overlay,
                )
            )
        for order, verdict in enumerate(out.verdict_rows, start=1):
            band = _band(next_row, verdict.task_key)
            emitted.append(
                build_result_row(run_id, next_row.digest, order, verdict, band)
            )
    if out.closed is not None:
        emitted.append(out.closed)
    return CodeEvalLegResult(
        next_row, out.row_changed, tuple(emitted), out.transition, out.dropped
    )


def _material(
    row: ModelCodeEvalRunRow, task_key: str, ports: CodeEvalLegPorts
) -> ModelCodeEvalTaskMaterial:
    candidate = _candidate_of(row, task_key)
    if candidate is None:
        raise LookupError(f"task {task_key} names no candidate of run {row.run_id}")
    (material,) = ports.suite_reader.read((candidate,))
    return material


def _band(row: ModelCodeEvalRunRow, task_key: str) -> str:
    for task in row.tasks:
        if task.task_key == task_key:
            return task.band
    return ""


def nudges_for(
    open_runs: frozenset[UUID], now: datetime, tick_id: str
) -> tuple[ModelCodeEvalRunNudge, ...]:
    """One nudge per open run, carrying the tick's time (the tick route)."""
    return tuple(
        ModelCodeEvalRunNudge(correlation_id=run_id, now=now, tick_id=tick_id)
        for run_id in sorted(open_runs, key=str)
    )


__all__ = [
    "CodeEvalLegConfig",
    "CodeEvalLegPorts",
    "CodeEvalLegResult",
    "nudges_for",
    "run_leg",
    "settings_for",
    "suite_digest",
    "to_message",
]
