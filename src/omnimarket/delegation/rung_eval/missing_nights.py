# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Find absent cells in an inclusive nightly window."""

from datetime import date, timedelta

from omnimarket.delegation.rung_eval.models import (
    ModelMissingNight,
    ModelRungEvalCase,
    ModelRungEvalRow,
    ModelRungSpec,
)


def detect_missing_nights(
    rows: list[ModelRungEvalRow],
    rungs: list[ModelRungSpec],
    cases: list[ModelRungEvalCase],
    through: date,
    window_days: int,
) -> list[ModelMissingNight]:
    present = {(r.night, r.rung_id, r.task_class, r.case_id) for r in rows}
    expected = {
        (through - timedelta(days=offset), rung.rung_id, case.task_class, case.case_id)
        for offset in range(window_days)
        for rung in rungs
        for case in cases
    }
    return [
        ModelMissingNight(night=night, rung_id=rung, task_class=task, case_id=case)
        for night, rung, task, case in sorted(expected - present)
    ]


def format_report(gaps: list[ModelMissingNight]) -> str:
    return "\n".join(
        f"MISSING {gap.night.isoformat()} | {gap.rung_id} | {gap.task_class} | {gap.case_id}"
        for gap in gaps
    )
