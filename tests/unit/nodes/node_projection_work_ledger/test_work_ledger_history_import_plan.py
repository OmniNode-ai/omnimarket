# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The history import plan: file order, legacy_ordinal, archive-import (OMN-21947, AC3)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from omnimarket.events.model_ledger_row_event import work_ledger_row_id
from omnimarket.nodes.node_projection_work_ledger.handlers.work_ledger_history_files import (
    read_ledger_history,
)
from omnimarket.nodes.node_projection_work_ledger.handlers.work_ledger_history_import import (
    ordering_key,
    plan_history_import,
)
from omnimarket.nodes.node_projection_work_ledger.models.model_work_ledger_history import (
    IMPORT_SOURCE,
    ModelHistoryImportPlan,
)

from .ledger_history_fixture import SimulatedLedger, two_day_ledger

pytestmark = pytest.mark.unit


def _plan(tmp_path: Path) -> tuple[SimulatedLedger, ModelHistoryImportPlan]:
    sim = two_day_ledger(tmp_path)
    live = sim.write()
    return sim, plan_history_import(read_ledger_history(live, live.parent / "archive"))


def test_plan_legacy_ordinal_is_file_order_of_splits_then_the_live_file(
    tmp_path: Path,
) -> None:
    sim, plan = _plan(tmp_path)
    archive = tmp_path / "archive"
    expected = [
        *(
            line
            for line in (archive / "ROLLING_WORK_LEDGER_2026-10-20-split.md")
            .read_text()
            .splitlines()
            if line.startswith("2026-")
        ),
        *sim.live_rows,
    ]

    assert [row.raw_row for row in plan.rows] == expected
    assert [row.legacy_ordinal for row in plan.rows] == list(
        range(1, len(expected) + 1)
    )
    assert plan.rows[0].file_key == "ROLLING_WORK_LEDGER_2026-10-20-split.md"
    assert plan.rows[-1].file_key == "live"


def test_plan_row_ids_are_the_projection_row_ids(tmp_path: Path) -> None:
    _, plan = _plan(tmp_path)

    assert all(row.row_id == work_ledger_row_id(row.raw_row) for row in plan.rows)
    assert len({row.row_id for row in plan.rows}) == len(plan.rows)


def test_plan_oldest_row_is_the_first_row_of_the_oldest_split(tmp_path: Path) -> None:
    _, plan = _plan(tmp_path)
    first = (
        (tmp_path / "archive" / "ROLLING_WORK_LEDGER_2026-10-20-split.md")
        .read_text()
        .splitlines()[3]
    )

    assert plan.oldest_file_key == "ROLLING_WORK_LEDGER_2026-10-20-split.md"
    assert plan.oldest_row_ts == datetime.strptime(
        first[:20], "%Y-%m-%dT%H:%M:%SZ"
    ).replace(tzinfo=UTC)
    assert plan.rows[0].row_ts == plan.oldest_row_ts


def test_plan_a_repeated_row_keeps_the_first_ordinal_and_is_counted(
    tmp_path: Path,
) -> None:
    sim = two_day_ledger(tmp_path)
    sim.live_rows.append(sim.live_rows[0])
    live = sim.write()
    plan = plan_history_import(read_ledger_history(live, live.parent / "archive"))

    assert plan.duplicate_rows == 1
    assert len({row.row_id for row in plan.rows}) == len(plan.rows)


def test_plan_names_the_import_source() -> None:
    assert IMPORT_SOURCE == "archive-import"


def test_ordering_contract_rows_without_a_sequence_first_by_ordinal_then_by_sequence() -> (
    None
):
    rows = [
        {"row_id": "d", "ledger_seq": 7, "legacy_ordinal": None},
        {"row_id": "b", "ledger_seq": None, "legacy_ordinal": 2},
        {
            "row_id": "e",
            "ledger_seq": 9,
            "legacy_ordinal": 5,
        },  # a bootstrap event carries both
        {"row_id": "a", "ledger_seq": None, "legacy_ordinal": 1},
        {"row_id": "c", "ledger_seq": 3, "legacy_ordinal": None},
    ]

    ordered = sorted(
        rows,
        key=lambda r: ordering_key(r["ledger_seq"], r["legacy_ordinal"], r["row_id"]),
    )

    assert [r["row_id"] for r in ordered] == ["a", "b", "c", "d", "e"]


def test_plan_a_legacy_row_type_is_imported_log_only_with_no_state(
    tmp_path: Path,
) -> None:
    sim = two_day_ledger(tmp_path)
    legacy = "2026-10-20T01:00:00Z | PROGRESS | lane=old-lane | ticket=OMN-9 | a legacy row type"
    sim.live_rows.insert(0, legacy)
    live = sim.write()

    plan = plan_history_import(read_ledger_history(live, live.parent / "archive"))

    row = next(r for r in plan.rows if r.raw_row == legacy)
    assert row.log_only
    assert (row.row_type, row.row_lane, row.tickets) == (
        "PROGRESS",
        "old-lane",
        ("OMN-9",),
    )
    assert plan.unprojectable_rows == (row.row_id,)
    assert not any(r.log_only for r in plan.rows if r.raw_row != legacy)
