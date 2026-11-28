# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The pure day-over-day change of the census daily record (typed request in, typed event out)."""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_work_ledger_file_census_effect.handlers.handler_daily_change import (
    HandlerDailyChange,
)
from omnimarket.nodes.node_work_ledger_file_census_effect.models import (
    ModelCensusDailyEventRequest,
    ModelCensusDailyRecord,
    ModelCensusDailyRow,
)

pytestmark = pytest.mark.unit


def _record(day: str, **rows: tuple[int, int, str]) -> ModelCensusDailyRecord:
    built = tuple(
        ModelCensusDailyRow(
            repository=name,
            own_line=False,
            files=files,
            runtime_files=runtime,
            ratchet=files,
            state=state,
        )
        for name, (files, runtime, state) in rows.items()
    )
    return ModelCensusDailyRecord(
        correlation_id=f"c-{day}",
        recorded_on=day,
        passed=True,
        total_files=sum(r.files for r in built),
        runtime_files=sum(r.runtime_files for r in built),
        repositories=built,
    )


def test_daily_record_change_is_zero_when_nothing_moved() -> None:
    day = _record("2026-11-23", a=(5, 1, "ok"))
    event = HandlerDailyChange().handle(
        ModelCensusDailyEventRequest(
            record=_record("2026-11-24", a=(5, 1, "ok")), previous=day, host="h"
        )
    )
    assert event.change.total_files_change == 0
    assert event.change.basis == "previous-day"


def test_daily_record_a_repository_new_since_the_previous_day_is_skipped_not_diffed() -> (
    None
):
    event = HandlerDailyChange().handle(
        ModelCensusDailyEventRequest(
            record=_record("2026-11-24", a=(5, 1, "ok"), b=(7, 2, "ok")),
            previous=_record("2026-11-23", a=(6, 1, "ok")),
            host="h",
        )
    )
    assert event.change.compared_repositories == ("a",)
    assert event.change.skipped_repositories == ("b",)
    assert event.change.total_files_change == -1


def test_daily_record_previous_record_of_the_same_or_a_later_day_is_not_a_previous_day() -> (
    None
):
    event = HandlerDailyChange().handle(
        ModelCensusDailyEventRequest(
            record=_record("2026-11-24", a=(5, 1, "ok")),
            previous=_record("2026-11-24", a=(9, 1, "ok")),
            host="h",
        )
    )
    assert event.change.basis == "no-previous-record"
    assert event.change.total_files_change is None
