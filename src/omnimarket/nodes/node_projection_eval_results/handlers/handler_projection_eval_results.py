# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Rule-7a pure fold of the eval-results projection (OMN-24171).

``handle(ModelEvalProjectionRequest) -> ModelEvalProjectionResult`` decides one delivery from the
rows the writer read for it (``read_plan`` names them): which scored items replace a standing
one, which trace parts are kept, what a close registers, and the recount of every rate the
delivery can change. It reads no clock, database, broker or envelope; replaying every event
gives the same rows.

Ordering authority: an item is replaced only by an event with a later ``scored_at``; at an equal
``scored_at`` the greater ``outcome_digest`` stands, so arrival order never decides.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from omnimarket.models.eval_item.model_eval_item_traced import (
    ModelEvalItemTracePart,
    assemble_trace,
)
from omnimarket.nodes.node_projection_eval_results.handlers.eval_results_mapping import (
    code_try_items,
    judged_item,
    live_week_run_id,
    run_record_of,
    trace_part_of,
)
from omnimarket.nodes.node_projection_eval_results.handlers.eval_results_rates import (
    recount_rates,
)
from omnimarket.nodes.node_projection_eval_results.models.model_eval_projection_io import (
    TOPIC_CODE_TRY,
    TOPIC_JUDGED,
    TOPIC_RUN_CLOSED,
    TOPIC_TRACE,
    ModelEvalProjectionRequest,
    ModelEvalProjectionResult,
    ModelEvalReadPlan,
    ModelEvalRefusedPart,
)
from omnimarket.nodes.node_projection_eval_results.models.model_eval_rows import (
    EnumEvalRefusal,
    ModelEvalItemRow,
    ModelEvalTraceRow,
)


def _judged_scope(payload: dict[str, Any]) -> tuple[str, str]:
    """The run and model a judged event counts into: the eval run, or the ISO week of a live call."""
    try:
        origin = str(payload["origin"])
        run_id = (
            str(payload["eval_run_id"])
            if origin == "eval"
            else live_week_run_id(datetime.fromisoformat(str(payload["call_time"])))
        )
        return run_id, str(payload["delegated_model_key"])
    except (KeyError, ValueError):
        return "", ""


class HandlerProjectionEvalResults:
    """Fold one delivery of any of the four source topics."""

    def read_plan(
        self, topic: str, payload: dict[str, Any], tenant_id: str
    ) -> ModelEvalReadPlan:
        """What the writer reads before calling ``handle``: the run (and model) or the scoring."""
        if topic == TOPIC_CODE_TRY:
            return ModelEvalReadPlan(
                tenant_id=tenant_id, run_id=str(payload.get("run_id", ""))
            )
        if topic == TOPIC_RUN_CLOSED:
            return ModelEvalReadPlan(
                tenant_id=tenant_id, run_id=str(payload.get("correlation_id", ""))
            )
        if topic == TOPIC_JUDGED:
            run_id, model_id = _judged_scope(payload)
            return ModelEvalReadPlan(
                tenant_id=str(payload.get("tenant_id", tenant_id)),
                run_id=run_id,
                model_id=model_id,
            )
        if topic == TOPIC_TRACE:
            return ModelEvalReadPlan(
                tenant_id=tenant_id, scoring_id=str(payload.get("scoring_id", ""))
            )
        return ModelEvalReadPlan(tenant_id=tenant_id)

    def handle(self, request: ModelEvalProjectionRequest) -> ModelEvalProjectionResult:
        if request.topic == TOPIC_TRACE:
            return self._trace(request)
        if request.topic == TOPIC_CODE_TRY:
            items, refused = code_try_items(request)
            run_id = str(request.payload.get("run_id", ""))
            return self._items(request, items, refused, run_id=run_id, model_id="")
        if request.topic == TOPIC_JUDGED:
            item, refusal = judged_item(request)
            if item is None:
                return ModelEvalProjectionResult(
                    refused=(refusal,) if refusal is not None else ()
                )
            return self._items(
                request,
                (item,),
                (),
                run_id=item.run_id,
                model_id=item.model_id,
                tenant_id=item.tenant_id,
            )
        if request.topic == TOPIC_RUN_CLOSED:
            return self._close(request)
        return ModelEvalProjectionResult(
            refused=(
                ModelEvalRefusedPart(
                    reason=EnumEvalRefusal.INVALID_EVENT,
                    subject=request.topic,
                    detail="not a source topic",
                ),
            )
        )

    def _items(
        self,
        request: ModelEvalProjectionRequest,
        incoming: tuple[ModelEvalItemRow, ...],
        refused: tuple[ModelEvalRefusedPart, ...],
        *,
        run_id: str,
        model_id: str,
        tenant_id: str | None = None,
    ) -> ModelEvalProjectionResult:
        tenant = tenant_id or request.tenant_id
        standing = {item.item_key: item for item in request.standing_items}
        changed: list[ModelEvalItemRow] = []
        for item in incoming:
            held = standing.get(item.item_key)
            if held is None or item.supersedes(held):
                standing[item.item_key] = item
                changed.append(item)
        scope = tuple(
            item
            for item in standing.values()
            if item.run_id == run_id and (not model_id or item.model_id == model_id)
        )
        return ModelEvalProjectionResult(
            items=tuple(changed),
            rates=recount_rates(
                tenant_id=tenant,
                run_id=run_id,
                items=scope,
                record=request.standing_run,
            )
            if run_id
            else (),
            refused=refused,
        )

    def _close(self, request: ModelEvalProjectionRequest) -> ModelEvalProjectionResult:
        record, refusal = run_record_of(request)
        if record is None:
            return ModelEvalProjectionResult(
                refused=(refusal,) if refusal is not None else ()
            )
        # The store replaces a stored close only for a later ``closed_at`` (UPSERT_RUN), so the fold
        # keeps that same winner and recounts from it: a late older close writes no run record and
        # leaves the stored rates as they stand, whatever order the two closes arrive in.
        standing = request.standing_run
        winner = (
            record
            if standing is None or standing.closed_at < record.closed_at
            else standing
        )
        scope = tuple(
            item for item in request.standing_items if item.run_id == winner.run_id
        )
        return ModelEvalProjectionResult(
            run_record=record if winner is record else None,
            rates=recount_rates(
                tenant_id=winner.tenant_id,
                run_id=winner.run_id,
                items=scope,
                record=winner,
            ),
        )

    def _trace(self, request: ModelEvalProjectionRequest) -> ModelEvalProjectionResult:
        part, refusal = trace_part_of(request)
        if part is None:
            return ModelEvalProjectionResult(
                refused=(refusal,) if refusal is not None else ()
            )
        scoring = str(part.scoring_id)
        held = {
            p.part_index: p
            for p in request.standing_parts
            if str(p.scoring_id) == scoring
        }
        existing = held.get(part.part_index)
        if existing is not None and existing.chunk_sha256 == part.chunk_sha256:
            return ModelEvalProjectionResult()
        everything = {
            index: ModelEvalItemTracePart(
                event_id=part.event_id,
                scoring_id=part.scoring_id,
                trace_sha256=row.trace_sha256,
                part_index=row.part_index,
                part_count=row.part_count,
                chunk_b64=row.chunk_b64,
                chunk_sha256=row.chunk_sha256,
            )
            for index, row in held.items()
        }
        everything[part.part_index] = part
        complete = len(everything) == part.part_count
        try:
            if existing is not None:
                raise ValueError(
                    f"part {part.part_index} arrived twice with different bytes"
                )
            if complete:
                assemble_trace(tuple(everything.values()))
            else:
                _agree(tuple(everything.values()))
        except ValueError as exc:
            return ModelEvalProjectionResult(
                drop_scoring_ids=(scoring,),
                refused=(
                    ModelEvalRefusedPart(
                        reason=EnumEvalRefusal.TRACE_DIGEST_MISMATCH,
                        subject=scoring,
                        detail=str(exc),
                    ),
                ),
            )
        return ModelEvalProjectionResult(
            traces=(
                ModelEvalTraceRow(
                    tenant_id=request.tenant_id,
                    item_key=str(part.event_id),
                    scoring_id=part.scoring_id,
                    part_index=part.part_index,
                    part_count=part.part_count,
                    trace_sha256=part.trace_sha256,
                    chunk_b64=part.chunk_b64,
                    chunk_sha256=part.chunk_sha256,
                    observed_at=request.observed_at,
                ),
            )
        )


def _agree(parts: tuple[ModelEvalItemTracePart, ...]) -> None:
    """Refuse a set of parts that disagree on the trace they belong to, before it is complete."""
    first = parts[0]
    for part in parts:
        for name in ("event_id", "scoring_id", "trace_sha256", "part_count"):
            if getattr(part, name) != getattr(first, name):
                raise ValueError(
                    f"parts disagree on {name}: {getattr(first, name)} vs {getattr(part, name)}"
                )


__all__ = ["HandlerProjectionEvalResults"]
