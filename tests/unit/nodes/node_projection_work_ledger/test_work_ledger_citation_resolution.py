# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``ledger_line_citation`` resolves a ``<ledger>:<line>`` citation as the file guard did (OMN-21947, AC4).

The file guard resolves the line against the live file as it stood when the citing row was
appended. A roll moves the head of the live file into a split, so the same line number names
a different row one generation later: the table keys a live citation on the roll generation
that existed when the citing row was written.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.events.model_ledger_row_event import work_ledger_row_id
from omnimarket.nodes.node_projection_work_ledger.handlers.work_ledger_citation import (
    citation_resolutions,
    resolve_in_table,
)
from omnimarket.nodes.node_projection_work_ledger.handlers.work_ledger_history_files import (
    read_ledger_history,
)
from omnimarket.nodes.node_projection_work_ledger.handlers.work_ledger_history_import import (
    plan_history_import,
)

from .ledger_history_fixture import LIVE_NAME, two_day_ledger

pytestmark = pytest.mark.unit


def test_citation_resolution_matches_the_file_guard_over_recorded_generations(
    tmp_path: Path,
) -> None:
    sim = two_day_ledger(tmp_path)
    live = sim.write()
    plan = plan_history_import(read_ledger_history(live, live.parent / "archive"))
    resolved = {
        (r.citing_row_id, r.line_no, r.file_key): r for r in citation_resolutions(plan)
    }

    assert len(sim.recorded) == 6  # a recording that is empty would pass everything
    checked = 0
    for recorded in sim.recorded:
        citing = work_ledger_row_id(recorded.citing_row)
        found = [
            r
            for (row_id, line, _), r in resolved.items()
            if row_id == citing and line == recorded.line
        ]
        assert len(found) == 1, recorded
        expected = (
            None
            if recorded.guard_saw is None
            else work_ledger_row_id(recorded.guard_saw)
        )
        assert found[0].row_id == expected, recorded
        checked += 1
    assert checked == len(sim.recorded)


def test_citation_resolution_keys_a_live_citation_by_the_generation_it_was_written_in(
    tmp_path: Path,
) -> None:
    sim = two_day_ledger(tmp_path)
    live = sim.write()
    plan = plan_history_import(read_ledger_history(live, live.parent / "archive"))
    lanes = {
        "lane-a": 0,  # generation 0
        "lane-c": 1,
        "lane-f": 2,
    }
    for resolution in citation_resolutions(plan):
        for lane, generation in lanes.items():
            if f"lane={lane} |" in next(
                row.raw_row
                for row in plan.rows
                if row.row_id == resolution.citing_row_id
            ):
                assert resolution.generation == generation
                assert resolution.file_key == "live"


def test_citation_resolution_same_line_number_names_different_rows_in_different_generations(
    tmp_path: Path,
) -> None:
    sim = two_day_ledger(tmp_path)
    live = sim.write()
    plan = plan_history_import(read_ledger_history(live, live.parent / "archive"))
    line = len(sim.preamble) + 2
    by_generation = {
        entry.generation: entry.row_id
        for entry in plan.citations
        if entry.file_key == "live" and entry.line_no == line
    }

    assert len(set(by_generation.values())) == len(by_generation) >= 2


def test_citation_resolution_a_split_citation_resolves_by_file_name_and_line(
    tmp_path: Path,
) -> None:
    sim = two_day_ledger(tmp_path)
    live = sim.write()
    plan = plan_history_import(read_ledger_history(live, live.parent / "archive"))
    split = "ROLLING_WORK_LEDGER_2026-10-20-split.md"
    lines = (live.parent / "archive" / split).read_text().splitlines()

    for number in (4, 9, 20):
        row_id = resolve_in_table(
            plan.citations, "rolling-work-ledger", split, 0, number
        )
        assert row_id == work_ledger_row_id(lines[number - 1]), number
    assert resolve_in_table(plan.citations, "rolling-work-ledger", split, 0, 1) is None
    assert (
        resolve_in_table(plan.citations, "rolling-work-ledger", split, 0, 99999) is None
    )


def test_citation_resolution_unresolved_citations_are_listed_not_dropped(
    tmp_path: Path,
) -> None:
    sim = two_day_ledger(tmp_path)
    live = sim.write()
    plan = plan_history_import(read_ledger_history(live, live.parent / "archive"))
    unresolved = [r for r in citation_resolutions(plan) if r.row_id is None]

    assert len(unresolved) == 2  # the preamble line and the line past the end
    assert all(r.file_key == "live" for r in unresolved)


def test_citation_resolution_ignores_a_stamp_that_only_looks_like_a_path(
    tmp_path: Path,
) -> None:
    sim = two_day_ledger(tmp_path)
    sim.append("ACK", "lane-z", "acks=2026-10-20T00:10:00Z")
    live = sim.write()
    plan = plan_history_import(read_ledger_history(live, live.parent / "archive"))

    assert (
        len(citation_resolutions(plan)) == len(sim.recorded) + 1
    )  # + the split citation
    assert LIVE_NAME == "ROLLING_WORK_LEDGER.md"


def test_citation_resolution_infers_a_header_shift_from_authority_citations(
    tmp_path: Path,
) -> None:
    from .ledger_history_fixture import SimulatedLedger

    sim = SimulatedLedger(tmp_path, preamble=("header", "", "---", "## section", ""))
    rulings = []
    for n in range(
        10
    ):  # an uneven pattern, so only the true shift lands on RULING rows
        if n in (0, 2, 5, 9):
            rulings.append(sim.append("RULING", "orchestrator", f"ticket=OMN-{n + 1}"))
        else:
            sim.append("STATUS", f"filler-{n}")
    for number in (
        6,
        8,
        11,
    ):  # the first three RULING rows, by their line in this header
        sim.append("CLAIM", "lane-x", f"consent=docs/tracking/{LIVE_NAME}:{number}")
    sim.roll(keep=2, split_name="ROLLING_WORK_LEDGER_2026-10-20-split.md")
    sim.preamble = ("header", "", "---", "## section")  # a roll dropped a header line
    sim.append("STATUS", "lane-after")
    live = sim.write()
    history = read_ledger_history(live, live.parent / "archive")

    unshifted = plan_history_import(history)
    shifted = plan_history_import(history, infer_shifts=True)

    assert shifted.header_shifts == {0: 1}
    wanted = {work_ledger_row_id(rulings[i]) for i in range(3)}
    assert {r.row_id for r in citation_resolutions(shifted)} == wanted
    assert {r.row_id for r in citation_resolutions(unshifted)} != wanted
    assert [r.guard_saw for r in sim.recorded] == rulings[:3]
