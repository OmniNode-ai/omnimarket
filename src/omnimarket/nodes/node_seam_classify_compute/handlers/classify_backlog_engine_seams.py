# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Classify backlog admission and lane engine policy seams."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import TYPE_CHECKING, cast

from omnimarket.models.model_backlog_item import (
    REPO_KINDS_CONTEXT_KEY,
    backlog_item_refusal,
)
from omnimarket.models.seam_reading import (
    EnumSeamCrossingState,
    ModelSeamCrossing,
    ModelSeamReading,
    ModelSeamWindow,
)
from omnimarket.nodes.node_lane_model_policy_compute.handlers.handler_lane_model_policy import (
    resolve_lane_model,
)
from omnimarket.nodes.node_seam_classify_compute.models.model_seam_classify_request import (
    ModelSeamClassifyRequest,
    ModelSeamClassifyResult,
)

if TYPE_CHECKING:
    from omnimarket.nodes.node_seam_classify_compute.handlers.handler_seam_classify import (
        SeamClassifier,
    )


def _sequence_fact(facts: Mapping[str, object], name: str) -> Sequence[object]:
    value = facts.get(name)
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise ValueError(f"{name} must be a sequence")
    return cast("Sequence[object]", value)


def _datetime_fact(facts: Mapping[str, object], name: str) -> datetime:
    value = facts.get(name)
    if not isinstance(value, str):
        raise ValueError(f"{name} must be an ISO 8601 string")
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO 8601 string") from exc


def _window(facts: Mapping[str, object]) -> ModelSeamWindow:
    return ModelSeamWindow(
        start=_datetime_fact(facts, "window_start"),
        end=_datetime_fact(facts, "window_end"),
    )


def _crossing_key(raw: object, index: int) -> str:
    if isinstance(raw, Mapping):
        key = raw.get("key")
        if isinstance(key, str) and key:
            return key
    return f"item-{index}"


def classify_sm_00(
    request: ModelSeamClassifyRequest,
) -> ModelSeamClassifyResult:
    """Read backlog items against the packaged admission policy."""
    items = _sequence_fact(request.facts, "items")
    window = _window(request.facts)
    repo_kinds: tuple[str, ...] | None = None
    if "repo_required_kinds" in request.facts:
        raw_kinds = _sequence_fact(request.facts, "repo_required_kinds")
        kinds: list[str] = []
        for kind in raw_kinds:
            if not isinstance(kind, str):
                raise ValueError("repo_required_kinds must contain only strings")
            kinds.append(kind)
        repo_kinds = tuple(kinds)

    crossings: list[ModelSeamCrossing] = []
    for index, item in enumerate(items):
        key = _crossing_key(item, index)
        if not isinstance(item, Mapping):
            crossings.append(
                ModelSeamCrossing(
                    key=key,
                    state=EnumSeamCrossingState.UNREADABLE,
                    reason="item must be a Mapping",
                )
            )
            continue

        reason = backlog_item_refusal(
            item,
            context=(
                {REPO_KINDS_CONTEXT_KEY: repo_kinds} if repo_kinds is not None else None
            ),
        )
        crossings.append(
            ModelSeamCrossing(
                key=key,
                state=(
                    EnumSeamCrossingState.MATCHED
                    if reason is None
                    else EnumSeamCrossingState.MISMATCHED
                ),
                reason=reason if reason is not None else "",
            )
        )

    return ModelSeamClassifyResult(
        readings=(
            ModelSeamReading.from_crossings(
                seam_id="SM-00",
                window=window,
                crossings=crossings,
                read_at=request.now,
            ),
        )
    )


def engine_read(
    *,
    required_engine: str | None,
    model: str,
    lane_kind: str,
    escalation_reason: str | None,
) -> bool:
    """Return whether the adopted policy permits the observed lane model."""
    return resolve_lane_model(
        requested_engine=model,
        required_engine=required_engine or None,
        required_model=model,
        lane_kind=lane_kind,
        code_edit_ready=True,
        escalation_reason=escalation_reason,
    ).allowed


def _classify_lanes(
    request: ModelSeamClassifyRequest,
    *,
    seam_id: str,
    model_fact: str,
) -> ModelSeamClassifyResult:
    lanes = _sequence_fact(request.facts, "lanes")
    window = _window(request.facts)
    crossings: list[ModelSeamCrossing] = []

    for index, raw in enumerate(lanes):
        key = _crossing_key(raw, index)
        reason = ""
        state = EnumSeamCrossingState.UNREADABLE
        if not isinstance(raw, Mapping):
            reason = "lane must be a Mapping"
        else:
            lane = cast("Mapping[str, object]", raw)
            kind = lane.get("kind")
            model = lane.get(model_fact)
            required_engine = lane.get("required_engine")
            escalation_reason = lane.get("escalation_reason")

            if not isinstance(kind, str) or not kind:
                reason = "lane kind must be a non-empty string"
            elif not isinstance(model, str) or not model:
                reason = f"lane {model_fact} must be a non-empty string"
            elif required_engine is not None and not isinstance(required_engine, str):
                reason = "lane required_engine must be a string or None"
            elif escalation_reason is not None and not isinstance(
                escalation_reason, str
            ):
                reason = "lane escalation_reason must be a string or None"
            elif engine_read(
                required_engine=required_engine,
                model=model,
                lane_kind=kind,
                escalation_reason=escalation_reason,
            ):
                state = EnumSeamCrossingState.MATCHED
            else:
                state = EnumSeamCrossingState.MISMATCHED
                reason = (
                    f"{model} not allowed for required engine "
                    f"{required_engine or 'any'}"
                )

        crossings.append(ModelSeamCrossing(key=key, state=state, reason=reason))

    return ModelSeamClassifyResult(
        readings=(
            ModelSeamReading.from_crossings(
                seam_id=seam_id,
                window=window,
                crossings=crossings,
                read_at=request.now,
            ),
        )
    )


def classify_sm_01(
    request: ModelSeamClassifyRequest,
) -> ModelSeamClassifyResult:
    """Read lane brief models against the adopted engine policy."""
    return _classify_lanes(request, seam_id="SM-01", model_fact="brief_model")


def classify_sm_02(
    request: ModelSeamClassifyRequest,
) -> ModelSeamClassifyResult:
    """Read lane CLAIM models against the adopted engine policy."""
    return _classify_lanes(request, seam_id="SM-02", model_fact="claim_model")


BACKLOG_ENGINE_OPERATIONS: dict[str, SeamClassifier] = {
    "classify_sm_00": classify_sm_00,
    "classify_sm_01": classify_sm_01,
    "classify_sm_02": classify_sm_02,
}
