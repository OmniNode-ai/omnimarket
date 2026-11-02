# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One test per class of difference between the file's and the database's verb answers (OMN-21947, AC1)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from omnimarket.nodes.node_projection_work_ledger.handlers.work_ledger_verb_adjudication import (
    adjudicate_answers,
    adjudicate_verb,
    classify_difference,
    parse_holds_answer,
    parse_open_claims_answer,
    render_adjudication,
)
from omnimarket.nodes.node_projection_work_ledger.models.model_work_ledger_history import (
    EnumVerbDiffClass as C,
)
from omnimarket.nodes.node_projection_work_ledger.models.model_work_ledger_history import (
    EnumVerbSide,
    ModelDiffWindow,
    ModelVerbItem,
)

pytestmark = pytest.mark.unit


def at(day: str) -> datetime:
    return datetime.fromisoformat(f"{day}T00:00:00+00:00").astimezone(UTC)


# the file read covers the live file only (since 10-30); the database's first row is 09-28
WINDOW = ModelDiffWindow(
    file_first_stamp=at("2026-10-30"),
    file_reads_archives=False,
    db_first_row_ts=at("2026-09-28"),
    as_of=at("2026-11-02"),
)


# open-claims always reads the archive splits, so for claims the file read has the whole history
CLAIM_WINDOW = WINDOW.model_copy(update={"file_reads_archives": True})


def hold(day: str, *, open_: bool = True) -> ModelVerbItem:
    return ModelVerbItem(
        verb="holds",
        key=f"{day}T10:00:00Z-lane-a",
        opened_at=at(day).replace(hour=10),
        until_open=open_,
    )


def claim(day: str, lane: str = "lane-a") -> ModelVerbItem:
    return ModelVerbItem(
        verb="open-claims",
        key=f"{lane}@{day}T10:00:00Z",
        opened_at=at(day).replace(hour=10),
    )


# --- holds -----------------------------------------------------------------------------------


def test_hold_class_file_window_truncation_database_only_hold_older_than_the_file_window() -> (
    None
):
    result = adjudicate_verb("holds", [], [hold("2026-10-25", open_=False)], WINDOW)

    (diff,) = result.diffs
    assert diff.diff_class is C.FILE_WINDOW_TRUNCATION
    assert diff.present_in is EnumVerbSide.DB
    assert diff.wrong_side is EnumVerbSide.FILE


def test_hold_class_file_window_truncation_needs_the_file_read_to_have_skipped_the_archives() -> (
    None
):
    archives = WINDOW.model_copy(update={"file_reads_archives": True})

    diff_class = classify_difference(
        hold("2026-10-25", open_=False), EnumVerbSide.DB, archives
    )

    assert diff_class is C.DATABASE_FOLD_DIFFERENCE


def test_hold_class_before_first_db_row_file_only_hold_older_than_the_database() -> (
    None
):
    result = adjudicate_verb("holds", [hold("2026-09-10", open_=False)], [], WINDOW)

    (diff,) = result.diffs
    assert diff.diff_class is C.BEFORE_FIRST_DB_ROW
    assert diff.present_in is EnumVerbSide.FILE
    assert diff.wrong_side is EnumVerbSide.DB


def test_hold_class_database_fold_difference_inside_both_extents() -> None:
    file_only = adjudicate_verb("holds", [hold("2026-10-31", open_=False)], [], WINDOW)
    db_only = adjudicate_verb("holds", [], [hold("2026-10-31", open_=False)], WINDOW)

    assert [d.diff_class for d in file_only.diffs] == [C.DATABASE_FOLD_DIFFERENCE]
    assert [d.diff_class for d in db_only.diffs] == [C.DATABASE_FOLD_DIFFERENCE]


def test_hold_class_stale_open_hold_is_listed_apart_with_its_age_and_is_no_difference() -> (
    None
):
    old = hold("2026-09-28")  # until=open, 34 days old at 11-02
    result = adjudicate_verb("holds", [], [old], WINDOW)

    assert result.diffs == ()
    assert result.agrees
    assert [(e.key, e.age_days) for e in result.stale] == [(old.key, 34)]
    lines = render_adjudication(result)
    assert any(line.startswith("STALE HOLDS") for line in lines)
    assert any("age=34d" in line for line in lines)


def test_hold_class_a_hold_with_a_limit_is_not_stale_whatever_its_age() -> None:
    limited = hold("2026-09-28", open_=False)

    result = adjudicate_verb("holds", [], [limited], WINDOW)

    assert result.stale == ()
    assert [d.diff_class for d in result.diffs] == [C.FILE_WINDOW_TRUNCATION]


def test_hold_class_exactly_fourteen_days_old_is_not_yet_stale() -> None:
    edge = hold("2026-10-19")  # 10:00 on 10-19 is 13 days 14 hours before 11-02T00:00

    assert adjudicate_verb("holds", [edge], [], WINDOW).stale == ()


def test_hold_class_without_a_stamp_is_unclassified_never_guessed() -> None:
    nameless = ModelVerbItem(
        verb="holds", key="legacy-hold", opened_at=None, until_open=True
    )

    result = adjudicate_verb("holds", [nameless], [], WINDOW)

    assert [d.diff_class for d in result.diffs] == [C.UNCLASSIFIED]


def test_hold_class_the_same_items_from_both_sources_agree() -> None:
    items = [hold("2026-10-31", open_=False), hold("2026-11-01", open_=False)]

    result = adjudicate_verb("holds", items, list(reversed(items)), WINDOW)

    assert result.agrees
    assert result.shared == 2


def test_hold_class_parses_the_reader_lines() -> None:
    answer = (
        "HOLD 2026-09-28T10:00:00Z-lane-a by=lane-a scope=<repo=omnibase_infra> until=open L12\n"
        "HOLD 2026-10-31T10:00:00Z-lane-b by=lane-b scope=<pr=x#1> until=2026-11-09T00:00:00Z "
        "ROLLING_WORK_LEDGER_2026-10-12-split.md:L7\n"
        "HOLD none checked=3 released=3 expired=0 legacy-no-id=0\n"
    )

    items = parse_holds_answer(answer)

    assert [(i.key, i.until_open) for i in items] == [
        ("2026-09-28T10:00:00Z-lane-a", True),
        ("2026-10-31T10:00:00Z-lane-b", False),
    ]
    assert items[0].opened_at == datetime(2026, 9, 28, 10, tzinfo=UTC)


# --- claims ----------------------------------------------------------------------------------


def test_claim_class_before_first_db_row_is_the_file_only_claim_older_than_the_database() -> (
    None
):
    result = adjudicate_verb("open-claims", [claim("2026-09-20")], [], CLAIM_WINDOW)

    (diff,) = result.diffs
    assert diff.diff_class is C.BEFORE_FIRST_DB_ROW
    assert diff.wrong_side is EnumVerbSide.DB


def test_claim_class_database_fold_difference_is_a_claim_inside_both_extents() -> None:
    result = adjudicate_verb(
        "open-claims",
        [claim("2026-10-10", "lane-a")],
        [claim("2026-10-11", "lane-b")],
        CLAIM_WINDOW,
    )

    assert (
        sorted(d.diff_class for d in result.diffs) == [C.DATABASE_FOLD_DIFFERENCE] * 2
    )
    assert {d.present_in for d in result.diffs} == {EnumVerbSide.FILE, EnumVerbSide.DB}


def test_claim_class_file_window_truncation_when_the_file_read_skipped_the_archives() -> (
    None
):
    result = adjudicate_verb("open-claims", [], [claim("2026-10-05")], WINDOW)

    assert [d.diff_class for d in result.diffs] == [C.FILE_WINDOW_TRUNCATION]


def test_claim_class_an_old_open_claim_is_never_stale() -> None:
    result = adjudicate_verb(
        "open-claims", [claim("2026-09-01")], [claim("2026-09-01")], CLAIM_WINDOW
    )

    assert result.stale == ()
    assert result.agrees


def test_claim_class_parses_the_reader_lines() -> None:
    answer = (
        "open-claims since=beginning claims=3 open=1 ledger-newest=2026-11-02T02:00:00Z now=x\n"
        "OPEN-CLAIM lane=lane-a claim=2026-10-10T10:00:00Z L99 ticket=OMN-1 newest-row=x kind=CLAIM\n"
    )

    (item,) = parse_open_claims_answer(answer)

    assert item.key == "lane-a@2026-10-10T10:00:00Z"
    assert item.opened_at == datetime(2026, 10, 10, 10, tzinfo=UTC)


def test_claim_class_counts_per_class_for_a_mixed_answer() -> None:
    file_text = (
        "OPEN-CLAIM lane=a claim=2026-09-01T10:00:00Z L1 x\n"
        "OPEN-CLAIM lane=b claim=2026-10-10T10:00:00Z L2 x\n"
        "OPEN-CLAIM lane=shared claim=2026-10-12T10:00:00Z L3 x\n"
    )
    db_text = (
        "OPEN-CLAIM lane=c claim=2026-10-11T10:00:00Z L1 x\n"
        "OPEN-CLAIM lane=shared claim=2026-10-12T10:00:00Z L2 x\n"
    )

    holds, claims = adjudicate_answers(("", ""), (file_text, db_text), WINDOW)

    assert holds.agrees
    assert claims.counts() == {
        "file_window_truncation": 0,
        "database_fold_difference": 2,
        "before_first_db_row": 1,
        "unclassified": 0,
    }
    assert claims.shared == 1
