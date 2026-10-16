# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Find absent cells in an inclusive nightly window.

An ``unresolved`` row records that a rung's environment was unset, so no case
was pinned to that rung. It is a placeholder, not an evaluation, and its cell is
reported as a gap with the row's detail. An ``error`` row means the rung was
called and failed, which is evidence, so it counts as present.
"""

from datetime import date, timedelta

from omnimarket.delegation.rung_eval.models import (
    EnumRungEvalStatus,
    ModelMissingNight,
    ModelRungEvalCase,
    ModelRungEvalRow,
    ModelRungSpec,
)

_ABSENT = "no row"


def detect_missing_nights(
    rows: list[ModelRungEvalRow],
    rungs: list[ModelRungSpec],
    cases: list[ModelRungEvalCase],
    through: date,
    window_days: int,
) -> list[ModelMissingNight]:
    present = {
        (r.night, r.rung_id, r.task_class, r.case_id)
        for r in rows
        if r.status is not EnumRungEvalStatus.UNRESOLVED
    }
    unresolved = {
        (r.night, r.rung_id, r.task_class, r.case_id): r.detail
        for r in rows
        if r.status is EnumRungEvalStatus.UNRESOLVED
    }
    expected = {
        (through - timedelta(days=offset), rung.rung_id, case.task_class, case.case_id)
        for offset in range(window_days)
        for rung in rungs
        for case in cases
    }
    return [
        ModelMissingNight(
            night=cell[0],
            rung_id=cell[1],
            task_class=cell[2],
            case_id=cell[3],
            reason=f"unresolved: {unresolved[cell]}" if cell in unresolved else _ABSENT,
        )
        for cell in sorted(expected - present)
    ]


def format_report(gaps: list[ModelMissingNight]) -> str:
    return "\n".join(
        f"MISSING {gap.night.isoformat()} | {gap.rung_id} | {gap.task_class} | {gap.case_id}"
        + ("" if gap.reason == _ABSENT else f" | {gap.reason}")
        for gap in gaps
    )
