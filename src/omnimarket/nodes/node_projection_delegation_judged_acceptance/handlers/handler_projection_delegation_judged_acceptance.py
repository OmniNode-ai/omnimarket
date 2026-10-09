# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Rule-7a pure fold of judged verdicts into acceptance cells.

No clock, database, broker or envelope. The result is a function of the set of
events: arrival order and redelivery change nothing. The window is measured back
from each cell's newest call time, never from the wall clock.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import timedelta
from uuid import UUID

from omnimarket.events import ModelDelegationAcceptanceJudgedEvent
from omnimarket.nodes.node_projection_delegation_judged_acceptance.models import (
    ModelJudgedAcceptanceCell,
    ModelJudgedAcceptanceFoldRequest,
    ModelJudgedAcceptanceFoldResult,
)

MIN_CELL_VERDICTS = 15
CALIBRATION_MIN_AGREEMENT = 0.75
CALIBRATION_MIN_KAPPA = 0.60
WINDOW_DAYS = 30
FOLDED_KINDS = frozenset({"task", "edit_loop_turn"})

_Z = 1.959963984540054

_VerdictKey = tuple[UUID, UUID, str]
_CellKey = tuple[UUID, str, str, str, str, str]


def _wilson_interval(accepts: int, n: int) -> tuple[float, float]:
    """Two-sided Wilson 95 percent interval for an accept proportion."""
    p = accepts / n
    denominator = 1 + _Z**2 / n
    center = (p + _Z**2 / (2 * n)) / denominator
    half = _Z * math.sqrt(p * (1 - p) / n + _Z**2 / (4 * n**2)) / denominator
    return max(0.0, center - half), min(1.0, center + half)


def _is_calibrated(event: ModelDelegationAcceptanceJudgedEvent) -> bool:
    return (
        event.calibration_agreement >= CALIBRATION_MIN_AGREEMENT
        and event.calibration_kappa >= CALIBRATION_MIN_KAPPA
    )


def _verdict_key(event: ModelDelegationAcceptanceJudgedEvent) -> _VerdictKey:
    return event.tenant_id, event.correlation_id, event.rubric_version


def _cell_key(event: ModelDelegationAcceptanceJudgedEvent) -> _CellKey:
    return (
        event.tenant_id,
        event.task_type,
        event.kind,
        event.delegated_tier,
        event.delegated_model_key,
        event.rubric_version,
    )


def _authority(event: ModelDelegationAcceptanceJudgedEvent) -> tuple[object, ...]:
    """Total order: latest judged_at, then greater judge_run_id, then content."""
    return event.judged_at, event.judge_run_id, event.model_dump_json()


def _winning_verdict(
    candidates: Sequence[ModelDelegationAcceptanceJudgedEvent],
) -> ModelDelegationAcceptanceJudgedEvent:
    """The verdict that stands for one call; independent of arrival order."""
    return max(candidates, key=_authority)


class HandlerProjectionDelegationJudgedAcceptance:
    """Fold judged acceptance events into one cell per model, task type and kind."""

    def handle(
        self, request: ModelJudgedAcceptanceFoldRequest
    ) -> ModelJudgedAcceptanceFoldResult:
        distinct = tuple(dict.fromkeys(request.events))

        excluded_kind = [e for e in distinct if e.kind not in FOLDED_KINDS]
        of_folded_kind = [e for e in distinct if e.kind in FOLDED_KINDS]
        refused = [e for e in of_folded_kind if not _is_calibrated(e)]
        calibrated = [e for e in of_folded_kind if _is_calibrated(e)]

        by_call: dict[_VerdictKey, list[ModelDelegationAcceptanceJudgedEvent]] = {}
        for event in calibrated:
            by_call.setdefault(_verdict_key(event), []).append(event)
        verdicts = [_winning_verdict(group) for group in by_call.values()]

        by_cell: dict[_CellKey, list[ModelDelegationAcceptanceJudgedEvent]] = {}
        for verdict in verdicts:
            by_cell.setdefault(_cell_key(verdict), []).append(verdict)

        cells: list[ModelJudgedAcceptanceCell] = []
        underpowered = 0
        folded = 0
        for key in sorted(by_cell, key=str):
            members = by_cell[key]
            newest = max(m.call_time for m in members)
            windowed = [
                m
                for m in members
                if m.call_time >= newest - timedelta(days=WINDOW_DAYS)
            ]
            if len(windowed) < MIN_CELL_VERDICTS:
                underpowered += 1
                continue
            folded += len(windowed)
            cells.append(_build_cell(key, windowed))

        return ModelJudgedAcceptanceFoldResult(
            cells=tuple(cells),
            folded_verdict_count=folded,
            uncalibrated_refused_count=len(refused),
            uncalibrated_judge_run_ids=tuple(sorted({e.judge_run_id for e in refused})),
            excluded_kind_count=len(excluded_kind),
            underpowered_cell_count=underpowered,
        )


def _build_cell(
    key: _CellKey, members: Sequence[ModelDelegationAcceptanceJudgedEvent]
) -> ModelJudgedAcceptanceCell:
    tenant_id, task_type, kind, tier, model_key, rubric_version = key
    n = len(members)
    accepts = sum(1 for m in members if m.accept)
    low, high = _wilson_interval(accepts, n)
    return ModelJudgedAcceptanceCell(
        tenant_id=tenant_id,
        task_type=task_type,
        kind=kind,
        delegated_tier=tier,
        delegated_model_key=model_key,
        rubric_version=rubric_version,
        n=n,
        accepts=accepts,
        accept_rate=accepts / n,
        wilson_low=low,
        wilson_high=high,
        mean_quality=sum(m.quality for m in members) / n,
        first_call_time=min(m.call_time for m in members),
        last_call_time=max(m.call_time for m in members),
        judge_run_count=len({m.judge_run_id for m in members}),
    )
