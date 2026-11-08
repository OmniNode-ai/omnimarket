# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Database fold differences, one test per cause, and the stale-hold rule (OMN-21947, AC1/AC2).

The database answers the read verbs by replaying its rows with the markdown reader's rules. Where
the two folds disagreed over the same rows, the database's replay was the wrong side; each test
here pins one such cause to the reader's rule.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from omnimarket.events.model_ledger_row_event import work_ledger_row_id
from omnimarket.models.work_ledger_query import ModelWorkLedgerRowRecord
from omnimarket.nodes.node_work_ledger_query_effect.handlers import (
    work_ledger_query_replay as replay,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 2, tzinfo=UTC)


def record(
    raw: str, *, seq: int | None = None, ordinal: int | None = None
) -> ModelWorkLedgerRowRecord:
    stamp = datetime.strptime(raw[:20], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    kind = raw.split(" | ")[1]
    return ModelWorkLedgerRowRecord(
        row_id=work_ledger_row_id(raw),
        row_ts=stamp,
        row_type=kind,
        text=raw,
        raw_row=raw,
        source="test",
        ledger_seq=seq,
        legacy_ordinal=ordinal,
    )


def rows(*raws: str, ordered: bool = False) -> list[replay.LedgerRow]:
    records = [
        record(raw, ordinal=n if ordered else None) for n, raw in enumerate(raws, 1)
    ]
    return replay.parse_rows(replay.in_contract_order(records) if ordered else records)


HOLD_ID = "2026-10-30T10:00:00Z-lane-a"


def hold_row(hold_id: str = HOLD_ID, *cells: str) -> str:
    return " | ".join(
        [hold_id[:20], "HOLD", "lane=lane-a", f"id={hold_id}", *cells, "text"]
    )


def release_row(re_: str, *cells: str) -> str:
    return " | ".join(
        ["2026-10-31T00:00:00Z", "RELEASE", "lane=lane-b", f"re={re_}", *cells, "text"]
    )


# --- holds -----------------------------------------------------------------------------------


def test_hold_class_database_fold_difference_scoped_release_frees_only_its_prs() -> (
    None
):
    raws = (
        hold_row(HOLD_ID, "pr=omnibase_infra#1,omnibase_infra#2"),
        release_row(HOLD_ID, "pr=omnibase_infra#1"),
    )

    shown, counts, stale = replay.holds_in_force(rows(*raws), now=NOW)

    assert [h.hold_id for h in shown] == [HOLD_ID]  # the old fold dropped it entirely
    assert shown[0].freed == ("omnibase_infra#1",)
    assert counts.released == 0
    assert stale == []


def test_hold_class_database_fold_difference_the_hold_does_not_stand_for_a_freed_pr() -> (
    None
):
    raws = (
        hold_row(HOLD_ID, "pr=omnibase_infra#1,omnibase_infra#2"),
        release_row(HOLD_ID, "pr=omnibase_infra#1"),
    )

    freed, _, _ = replay.holds_in_force(rows(*raws), now=NOW, pr="omnibase_infra#1")
    kept, _, _ = replay.holds_in_force(rows(*raws), now=NOW, pr="omnibase_infra#2")

    assert freed == []
    assert [h.hold_id for h in kept] == [HOLD_ID]


def test_hold_class_database_fold_difference_scoped_releases_covering_every_pr_end_it() -> (
    None
):
    raws = (
        hold_row(HOLD_ID, "pr=omnibase_infra#1,omnibase_infra#2"),
        release_row(HOLD_ID, "pr=omnibase_infra#1"),
        release_row(HOLD_ID, "pr=omnibase_infra#2"),
    )

    shown, counts, _ = replay.holds_in_force(rows(*raws), now=NOW)

    assert shown == []
    assert counts.released == 1


def test_hold_class_database_fold_difference_a_plain_release_still_ends_the_hold() -> (
    None
):
    raws = (hold_row(HOLD_ID, "repo=omnibase_infra"), release_row(HOLD_ID))

    shown, counts, _ = replay.holds_in_force(rows(*raws), now=NOW)

    assert shown == []
    assert counts.released == 1


def test_hold_class_database_fold_difference_a_multi_pr_hold_matches_each_listed_pr() -> (
    None
):
    raws = (hold_row(HOLD_ID, "pr=omnibase_infra#1,omnibase_infra#2"),)

    shown, _, _ = replay.holds_in_force(rows(*raws), now=NOW, pr="omnibase_infra#2")

    assert [h.hold_id for h in shown] == [HOLD_ID]


def test_hold_class_stale_open_hold_leaves_the_holds_in_force_and_is_listed_with_its_age() -> (
    None
):
    old_id = "2026-09-28T10:00:00Z-lane-a"
    raws = (
        hold_row(old_id, "repo=omnibase_infra"),
        hold_row(HOLD_ID, "repo=omnibase_infra"),
    )

    shown, counts, stale = replay.holds_in_force(rows(*raws), now=NOW)

    assert [h.hold_id for h in shown] == [HOLD_ID]
    assert [(s.hold.hold_id, s.age_days) for s in stale] == [(old_id, 34)]
    assert counts.checked == 2


def test_hold_class_a_hold_with_a_limit_is_never_stale() -> None:
    old_id = "2026-09-28T10:00:00Z-lane-a"
    raws = (hold_row(old_id, "repo=omnibase_infra", "until=2026-12-01T00:00:00Z"),)

    shown, _, stale = replay.holds_in_force(rows(*raws), now=NOW)

    assert [h.hold_id for h in shown] == [old_id]
    assert stale == []


def test_hold_class_a_partly_released_old_hold_is_not_stale() -> None:
    old_id = "2026-09-28T10:00:00Z-lane-a"
    raws = (
        hold_row(old_id, "pr=omnibase_infra#1,omnibase_infra#2"),
        release_row(old_id, "pr=omnibase_infra#1"),
    )

    shown, _, stale = replay.holds_in_force(rows(*raws), now=NOW)

    assert [h.hold_id for h in shown] == [old_id]
    assert stale == []


# --- claims ----------------------------------------------------------------------------------


def claim_row(stamp: str, lane: str = "lane-a") -> str:
    return f"{stamp} | CLAIM | lane={lane} | ticket=OMN-1 | text"


def terminal_row(stamp: str, lane: str = "lane-a") -> str:
    return f"{stamp} | TERMINAL | lane={lane} | outcome=done | text"


def test_claim_class_database_fold_difference_a_terminal_that_arrived_first_does_not_close() -> (
    None
):
    # arrival order: the TERMINAL (10:05) was appended before the CLAIM (10:00); the reader
    # folds in arrival order and so leaves the claim open. A stamp-ordered fold closed it.
    raws = (terminal_row("2026-10-31T10:05:00Z"), claim_row("2026-10-31T10:00:00Z"))

    by_arrival = replay.open_claims(rows(*raws, ordered=True))
    by_stamp = replay.open_claims(
        rows(*sorted(raws))  # what ORDER BY row_ts returned
    )

    assert [c.lane for c in by_arrival] == ["lane-a"]
    assert by_stamp == []


def test_claim_class_ordering_contract_no_sequence_first_by_ordinal_then_by_sequence() -> (
    None
):
    first = record("2026-10-01T00:00:00Z | STATUS | lane=a | text", ordinal=2)
    second = record("2026-10-01T00:00:01Z | STATUS | lane=b | text", ordinal=1)
    third = record("2026-10-01T00:00:02Z | STATUS | lane=c | text", seq=5)
    fourth = record("2026-10-01T00:00:03Z | STATUS | lane=d | text", seq=9, ordinal=7)
    unnumbered = record("2026-10-01T00:00:04Z | STATUS | lane=e | text")

    ordered = replay.in_contract_order([fourth, third, unnumbered, first, second])

    assert [r.row_lane or r.text.split("lane=")[1][0] for r in ordered] == [
        "b",
        "a",
        "e",
        "c",
        "d",
    ]
    assert [r.text.split("lane=")[1][0] for r in ordered] == ["b", "a", "e", "c", "d"]


def test_claim_class_rows_with_no_ordering_fields_keep_the_order_they_came_in() -> None:
    raws = [claim_row(f"2026-10-31T10:0{n}:00Z", f"lane-{n}") for n in (3, 1, 2)]

    ordered = replay.in_contract_order([record(raw) for raw in raws])

    assert [r.text for r in ordered] == raws
