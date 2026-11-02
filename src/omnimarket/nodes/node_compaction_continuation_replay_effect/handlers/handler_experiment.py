# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Three repeats per context, the existing CM.3 classifier, and matched exclusions."""

from __future__ import annotations

import hashlib
import re
import tempfile
from datetime import datetime
from pathlib import Path

from omnibase_core.models.container.model_onex_container import ModelONEXContainer

from omnimarket.models.compaction_measurement.action import (
    EnumEventSource,
    EnumOpenLoopKind,
    ModelActionScoreRequest,
    ModelContinuationEvent,
    ModelOpenLoopItem,
)
from omnimarket.models.compaction_measurement.ground_truth import ModelGroundTruthResult

from ..container.container_replay import ContainerContinuationReplay
from ..models import (
    EnumRefusalReason,
    EnumReplayStatus,
    ModelRecordedCall,
    ModelReplayRequest,
    ModelReplayResult,
    ModelStabilityCell,
    ModelStabilityRequest,
)
from ..models.model_experiment import (
    ModelExperimentRequest,
    ModelExperimentResult,
    ModelGradedRepeat,
)


def _with_item_keys(
    result: ModelReplayResult, truth: ModelGroundTruthResult
) -> ModelReplayResult:
    """Resolve lane names in proposals to the canonical item keys used in both arms."""
    calls: list[ModelRecordedCall] = []
    keys: set[str] = set()
    for call in result.calls:
        text = " ".join((call.call.command, call.call.row, call.call.path))
        matched = []
        for item in truth.items:
            names = "|".join(re.escape(n) for n in (item.key, *item.required_spans))
            if call.item_key == item.key or re.search(
                rf"(?<![\w-])(?:{names})(?![\w-])", text, re.IGNORECASE
            ):
                matched.append(item.key)
        item_keys = sorted(set(matched)) or [call.item_key]
        keys.update(f"{call.call_class}|{key}" for key in item_keys)
        calls.append(call.model_copy(update={"item_key": item_keys[0]}))
    return result.model_copy(
        update={
            "calls": calls,
            "proposed_call_set": sorted(keys),
            "refusal_log": [c for c in calls if c.refusal_reason is not None],
        }
    )


class HandlerReplayExperiment:
    def __init__(self, container: ModelONEXContainer) -> None:
        self._container = container

    def handle(self, request: ModelExperimentRequest) -> ModelExperimentResult:
        container = self._container.get_service(ContainerContinuationReplay)
        # Validate calibration data before model calls or scratch writes.
        tolerance_before = Path(request.tolerance_file).read_bytes()
        container.stability.validate_policy(request.tolerance_file)
        container.scratch_root.mkdir(parents=True, exist_ok=True)
        run_root = Path(
            tempfile.mkdtemp(prefix="continuation-", dir=container.scratch_root)
        )
        handler = container.replay_handler(
            container.stubs_factory, model=container.model
        )
        ledger_snapshots = {
            c.ledger_source: Path(c.ledger_source).read_bytes()
            for c in request.contexts
            if c.ledger_source is not None
        }
        repeats: list[ModelGradedRepeat] = []
        cells: list[ModelStabilityCell] = []
        for context in request.contexts:
            call_sets: list[list[str]] = []
            for repeat in range(container.policy.repeats):
                result = handler.handle(
                    ModelReplayRequest(
                        correlation_id=request.correlation_id,
                        boundary_id=context.case.boundary_uuid,
                        arm=context.arm,
                        repeat_index=repeat,
                        n_turns=request.n_turns,
                        scratch_dir=str(run_root),
                        ledger_source=context.ledger_source,
                        ledger_content=ledger_snapshots.get(context.ledger_source)
                        if context.ledger_source
                        else None,
                        frozen_files=context.frozen_files,
                        frozen_bytes=context.frozen_bytes,
                        scratch_worktree=context.scratch_worktree,
                        context=context.context,
                        continuation_prompt=request.continuation_prompt,
                        model_id=request.answering_model_id,
                        temperature=container.policy.temperature
                        if request.temperature is None
                        else request.temperature,
                    )
                )
                result = _with_item_keys(result, context.ground_truth)
                if (
                    result.status is EnumReplayStatus.COMPLETED
                    and result.turns_run == request.n_turns
                ):
                    call_sets.append(result.proposed_call_set)
                events = []
                for turn in range(1, result.turns_run + 1):
                    events.append(
                        ModelContinuationEvent(
                            turn=turn,
                            source=EnumEventSource.ASSISTANT_TEXT,
                            text=result.assistant_text[turn - 1],
                        )
                    )
                    for call in (c for c in result.calls if c.turn == turn):
                        refused = None
                        if call.refusal_reason is not None:
                            refused = (
                                "malformed"
                                if call.refusal_reason
                                in {
                                    EnumRefusalReason.MALFORMED,
                                    EnumRefusalReason.EGRESS_BLOCKED,
                                }
                                else "by_design"
                            )
                        events.append(
                            ModelContinuationEvent(
                                turn=turn,
                                source=EnumEventSource.TOOL_CALL,
                                text=" ".join(
                                    (
                                        call.call.command,
                                        call.call.row,
                                        call.call.path,
                                        call.item_key,
                                    )
                                ),
                                call_class=call.call_class,
                                refused=refused,
                            )
                        )
                items = tuple(
                    ModelOpenLoopItem(
                        key=item.key,
                        kind=EnumOpenLoopKind(item.kind.value),
                        names=tuple(dict.fromkeys((item.key, *item.required_spans))),
                    )
                    for item in context.ground_truth.items
                    if item.kind.value in set(EnumOpenLoopKind)
                )
                if context.action_items is not None:
                    items = context.action_items
                score = container.scorer.handle(
                    ModelActionScoreRequest(
                        continuation_id=f"{request.correlation_id}:{context.case.boundary_uuid}:{context.arm}:{repeat}",
                        judged_at=datetime.fromisoformat(
                            context.ground_truth.now.replace("Z", "+00:00")
                        ),
                        items=items,
                        events=tuple(events),
                        policy=request.policy,
                        holds=context.holds,
                        horizons=tuple(
                            sorted(
                                {
                                    container.policy.default_turns,
                                    *container.policy.sensitivity_turns,
                                    request.n_turns,
                                }
                            )
                        ),
                        headline_horizon=request.n_turns,
                    )
                )
                repeats.append(
                    ModelGradedRepeat(
                        replay=result, action_score=score, error=result.failure_reason
                    )
                )
            cells.append(
                ModelStabilityCell(
                    boundary_id=context.case.boundary_uuid,
                    arm=context.arm,
                    repeats=call_sets,
                )
            )
        if Path(request.tolerance_file).read_bytes() != tolerance_before:
            raise ValueError("calibration data changed during the experiment")
        stability = container.stability.handle(
            ModelStabilityRequest(
                correlation_id=request.correlation_id,
                cells=cells,
                tolerance_file=request.tolerance_file,
            )
        )
        return ModelExperimentResult(
            correlation_id=request.correlation_id,
            model_id=request.answering_model_id,
            prompt_sha256=hashlib.sha256(
                request.continuation_prompt.encode()
            ).hexdigest(),
            tolerance_sha256=hashlib.sha256(tolerance_before).hexdigest(),
            repeats=repeats,
            stability=stability,
            retained_cells=sorted(
                (c.boundary_id, c.arm) for c in stability.cells if c.retained
            ),
        )
