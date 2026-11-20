# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Map the source events onto the scored-item record and the run record (OMN-24171, plan 5.1). Pure.

A code try row (``ModelAttemptReductionRow``, row kind try) maps to scorer ``contract_checks``;
a judged event (``ModelDelegationAcceptanceJudgedEvent``) maps to scorer ``reviewer_model``. The
code row carries no time of its own, so the envelope's event time is its ``scored_at``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from omnimarket.enums.enum_eval_engine import EnumEvalEngine
from omnimarket.enums.enum_eval_scorer import EnumEvalScorer
from omnimarket.enums.enum_eval_step_verdict import (
    EnumEvalStepVerdict,
    EnumEvalStepVerdictSource,
)
from omnimarket.enums.enum_eval_unscorable_reason import EnumEvalUnscorableReason
from omnimarket.events.context_roi import ModelAttemptReductionRow
from omnimarket.models.context_roi_trial_fields import EnumContextRoiRowKind
from omnimarket.models.delegation.model_size_band import EnumSizeBand
from omnimarket.models.delegation_acceptance_judge.enum_acceptance_origin import (
    EnumAcceptanceOrigin,
)
from omnimarket.models.delegation_acceptance_judge.model_delegation_acceptance_judged_event import (
    ModelDelegationAcceptanceJudgedEvent,
)
from omnimarket.models.enum_code_score import (
    EnumCodeScoreSource,
    EnumCodeScoreVerdict,
)
from omnimarket.models.eval_item.model_eval_item_scored import ModelEvalItemScored
from omnimarket.models.eval_item.model_eval_item_traced import ModelEvalItemTracePart
from omnimarket.nodes.node_projection_eval_results.models.model_eval_projection_io import (
    ModelEvalProjectionRequest,
    ModelEvalRefusedPart,
)
from omnimarket.nodes.node_projection_eval_results.models.model_eval_rows import (
    EnumEvalRefusal,
    ModelEvalItemRow,
    ModelEvalRunRecord,
    ModelEvalRunSequence,
)

CODE_TASK_SET_ID = "code_eval_suite"
LIVE_TASK_SET_ID = "live"
JUDGED_EVAL_TASK_SET_ID = "judged_eval"
LIVE_RUN_PREFIX = "live-"

#: The providers a code try row names. A provider outside this table is refused, never labelled as
#: another engine: a hosted model stored as local would lose the hosted note on Chart 1.
_PROVIDER_ENGINE = {
    "local": EnumEvalEngine.LOCAL,
    "glm": EnumEvalEngine.GLM,
    "codex": EnumEvalEngine.CODEX,
    "claude": EnumEvalEngine.CLAUDE,
    "anthropic": EnumEvalEngine.CLAUDE,
}
#: The delegation tiers a judged event names. ``cheap_cloud`` is the hosted tier the registry
#: routes to GLM; a tier outside this table is refused, never labelled as another engine.
_TIER_ENGINE = {
    "local": EnumEvalEngine.LOCAL,
    "cheap_cloud": EnumEvalEngine.GLM,
    "glm": EnumEvalEngine.GLM,
    "codex": EnumEvalEngine.CODEX,
    "claude": EnumEvalEngine.CLAUDE,
}
#: Every code-score source is an infrastructure fault but one: a draft that could not be recovered
#: is the model's own output (plan 7.6.3).
_SOURCE_REASON = {
    EnumCodeScoreSource.DRAFT_NOT_RECOVERABLE: EnumEvalUnscorableReason.MALFORMED_ANSWER,
}


def live_week_run_id(call_time: datetime) -> str:
    """The ISO week of ``call_time`` as a live run id, ``live-2026-W46``."""
    iso = call_time.isocalendar()
    return f"{LIVE_RUN_PREFIX}{iso.year}-W{iso.week:02d}"


def is_live_run(run_id: str) -> bool:
    return run_id.startswith(LIVE_RUN_PREFIX)


def _lane_stamps(request: ModelEvalProjectionRequest) -> dict[str, Any]:
    return {
        "lane": request.lane,
        "host": request.host,
        "install_revision": request.install_revision,
    }


def code_try_items(
    request: ModelEvalProjectionRequest,
) -> tuple[tuple[ModelEvalItemRow, ...], tuple[ModelEvalRefusedPart, ...]]:
    """The scored items of one run-completed event: one per try row, controls and unscored rows refused."""
    try:
        rows = tuple(request.payload.get("rows", ()))
        run_id = str(request.payload["run_id"])
    except KeyError as exc:
        return (), (
            ModelEvalRefusedPart(
                reason=EnumEvalRefusal.INVALID_EVENT, subject="run", detail=str(exc)
            ),
        )
    items: list[ModelEvalItemRow] = []
    refused: list[ModelEvalRefusedPart] = []
    for raw in rows:
        try:
            row = ModelAttemptReductionRow.model_validate(raw)
        except ValidationError as exc:
            refused.append(
                ModelEvalRefusedPart(
                    reason=EnumEvalRefusal.INVALID_EVENT,
                    subject=f"{run_id}:row",
                    detail=str(exc.errors()[0]["msg"]),
                )
            )
            continue
        subject = f"{run_id}:{row.task_id}:{row.model_id}:{row.try_ordinal}"
        if row.row_kind is not EnumContextRoiRowKind.TRY:
            refused.append(
                ModelEvalRefusedPart(reason=EnumEvalRefusal.NOT_A_TRY, subject=subject)
            )
            continue
        if row.check_verdict is None:
            refused.append(
                ModelEvalRefusedPart(reason=EnumEvalRefusal.UNSCORED, subject=subject)
            )
            continue
        unscorable = row.check_verdict is EnumCodeScoreVerdict.UNSCORABLE
        reason = None
        if unscorable:
            reason = (
                _SOURCE_REASON.get(row.unscorable_source)
                if row.unscorable_source is not None
                else None
            ) or EnumEvalUnscorableReason.CHECK_COULD_NOT_RUN
        passed: bool | None = (
            None if unscorable else row.check_verdict is EnumCodeScoreVerdict.PASS
        )
        engine = _PROVIDER_ENGINE.get(row.provider)
        if engine is None:
            refused.append(
                ModelEvalRefusedPart(
                    reason=EnumEvalRefusal.UNMAPPED_PROVIDER,
                    subject=subject,
                    detail=row.provider,
                )
            )
            continue
        try:
            item = ModelEvalItemScored.model_validate(
                {
                    "tenant_id": request.tenant_id,
                    "run_id": run_id,
                    "experiment_id": row.experiment_id,
                    "replicate": row.replicate,
                    "try_ordinal": row.try_ordinal,
                    "task_set_id": CODE_TASK_SET_ID,
                    "task_set_digest": row.suite_digest,
                    "task_id": row.task_id,
                    "task_type": "code",
                    "size_band": row.size_band,
                    "engine": engine,
                    "model_id": row.model_id,
                    "model_revision": row.model_revision,
                    "generation_settings_hash": row.generation_settings_hash,
                    "prompt_template_version": row.prompt_template_version,
                    "context_arm": row.context_factor_subset,
                    "context_pack_hash": row.context_pack_hash,
                    "pack_profile_hash": row.factor_subset_hash,
                    "prompt_tokens": row.prompt_tokens,
                    "scorer": EnumEvalScorer.CONTRACT_CHECKS,
                    "scorer_version": row.scorer_version,
                    "passed": passed,
                    "unscorable_reason": reason,
                    "step_verdict": (
                        EnumEvalStepVerdict.NONE
                        if passed is None
                        else EnumEvalStepVerdict.ACCEPT
                        if passed
                        else EnumEvalStepVerdict.REJECT
                    ),
                    "step_verdict_source": EnumEvalStepVerdictSource.DETERMINISTIC,
                    "scored_at": request.observed_at,
                    **_lane_stamps(request),
                }
            )
        except ValidationError as exc:
            refused.append(
                ModelEvalRefusedPart(
                    reason=EnumEvalRefusal.INVALID_ITEM,
                    subject=subject,
                    detail=str(exc.errors()[0]["msg"]),
                )
            )
            continue
        items.append(ModelEvalItemRow.of(item))
    return tuple(items), tuple(refused)


def judged_item(
    request: ModelEvalProjectionRequest,
) -> tuple[ModelEvalItemRow | None, ModelEvalRefusedPart | None]:
    """The scored item of one judged event, or the refusal that explains why it is none."""
    try:
        event = ModelDelegationAcceptanceJudgedEvent.model_validate(request.payload)
    except ValidationError as exc:
        first = exc.errors()[0]
        field = ".".join(str(part) for part in first["loc"])
        return None, ModelEvalRefusedPart(
            reason=EnumEvalRefusal.INVALID_EVENT,
            subject="judged",
            detail=f"{field}: {first['msg']}" if field else str(first["msg"]),
        )
    except KeyError as exc:
        # An event with no ``event_id`` derives it in a ``default_factory`` that reads
        # ``judge_run_id`` and ``correlation_id`` from the fields validated so far. When either
        # field failed its own check it is absent there, and pydantic lets the KeyError through
        # instead of folding it into a ValidationError. It is a malformed event like any other.
        return None, ModelEvalRefusedPart(
            reason=EnumEvalRefusal.INVALID_EVENT,
            subject="judged",
            detail=(
                f"{exc.args[0]}: missing or invalid, so event_id cannot be derived"
                if exc.args
                else "event_id cannot be derived"
            ),
        )
    subject = str(event.correlation_id)
    if event.kind != "task":
        return None, ModelEvalRefusedPart(
            reason=EnumEvalRefusal.NOT_A_TASK, subject=subject
        )
    engine = _TIER_ENGINE.get(event.delegated_tier)
    if engine is None:
        return None, ModelEvalRefusedPart(
            reason=EnumEvalRefusal.UNMAPPED_TIER,
            subject=subject,
            detail=event.delegated_tier,
        )
    live = event.origin is EnumAcceptanceOrigin.LIVE
    run_id = live_week_run_id(event.call_time) if live else str(event.eval_run_id)
    try:
        item = ModelEvalItemScored.model_validate(
            {
                "tenant_id": str(event.tenant_id),
                "run_id": run_id,
                "try_ordinal": 1,
                "task_set_id": LIVE_TASK_SET_ID if live else JUDGED_EVAL_TASK_SET_ID,
                "task_set_digest": "",
                "task_id": subject,
                "task_type": event.task_type,
                # The judged event carries no size. The live rate row ignores the band (size band all).
                "size_band": EnumSizeBand.S,
                "engine": engine,
                "model_id": event.delegated_model_key,
                "context_arm": "unknown",
                "scorer": EnumEvalScorer.REVIEWER_MODEL,
                "scorer_version": f"{event.rubric_version}+{event.judge_model_version}",
                "passed": event.accept,
                "step_verdict": (
                    EnumEvalStepVerdict.ACCEPT
                    if event.accept
                    else EnumEvalStepVerdict.REJECT
                ),
                "step_verdict_source": EnumEvalStepVerdictSource.REVIEWER_MODEL,
                "scored_at": event.judged_at,
                **_lane_stamps(request),
            }
        )
    except ValidationError as exc:
        return None, ModelEvalRefusedPart(
            reason=EnumEvalRefusal.INVALID_ITEM,
            subject=subject,
            detail=str(exc.errors()[0]["msg"]),
        )
    return ModelEvalItemRow.of(item), None


def trace_part_of(
    request: ModelEvalProjectionRequest,
) -> tuple[ModelEvalItemTracePart | None, ModelEvalRefusedPart | None]:
    """The trace part of one delivery, or the refusal that explains why it is none."""
    try:
        return ModelEvalItemTracePart.model_validate(request.payload), None
    except ValidationError as exc:
        return None, ModelEvalRefusedPart(
            reason=EnumEvalRefusal.INVALID_EVENT,
            subject="trace",
            detail=str(exc.errors()[0]["msg"]),
        )


class _ClosedSequenceWire(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    task_key: str
    model: str
    band: str = ""


class _ClosedWire(BaseModel):
    """The run-close event as the projection reads it: tolerant of fields a later producer adds.

    ``ModelCodeEvalRunClosed`` on dev carries ``sequences`` and ``void_reason`` but not ``series``,
    ``task_sample_digest``, ``item_count``, ``band_usable`` or ``band_target``; they are read when
    present. Without ``item_count`` the first tries the run closed over are its sequences.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    correlation_id: str
    closed_at: datetime
    abandoned: bool = False
    void_reason: str | None = None
    sequences: tuple[_ClosedSequenceWire, ...] = ()
    series: str = ""
    task_sample_digest: str = ""
    item_count: int | None = Field(default=None, ge=0)
    band_usable: dict[str, int] = Field(default_factory=dict)
    band_target: dict[str, int] = Field(default_factory=dict)


def run_record_of(
    request: ModelEvalProjectionRequest,
) -> tuple[ModelEvalRunRecord | None, ModelEvalRefusedPart | None]:
    """The run record of one close event, or the refusal."""
    try:
        wire = _ClosedWire.model_validate(request.payload)
    except ValidationError as exc:
        return None, ModelEvalRefusedPart(
            reason=EnumEvalRefusal.INVALID_EVENT,
            subject="run-closed",
            detail=str(exc.errors()[0]["msg"]),
        )
    sequences = tuple(
        ModelEvalRunSequence(task_id=s.task_key, model_id=s.model, band=s.band)
        for s in wire.sequences
    )
    return (
        ModelEvalRunRecord(
            tenant_id=request.tenant_id,
            run_id=wire.correlation_id,
            series=wire.series,
            task_sample_digest=wire.task_sample_digest,
            closed_at=wire.closed_at,
            abandoned=wire.abandoned,
            void_reason=wire.void_reason or ("abandoned" if wire.abandoned else ""),
            item_count=len(sequences) if wire.item_count is None else wire.item_count,
            sequences=sequences,
            band_usable=wire.band_usable,
            band_target=wire.band_target,
        ),
        None,
    )


__all__ = [
    "CODE_TASK_SET_ID",
    "LIVE_RUN_PREFIX",
    "code_try_items",
    "is_live_run",
    "judged_item",
    "live_week_run_id",
    "run_record_of",
    "trace_part_of",
]
