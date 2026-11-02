# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for the claim index over the ledger, on synthetic ledgers only.

The ledger is the store: append-only, already serialised, already carrying the lane identifier. A
hook cannot read thousands of growing lines on every push, so this is the derived index, and the
three properties that keep a cache from becoming a second, disagreeing store are what most of these
tests pin.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from omnimarket.handlers import claim_index as ci

pytestmark = pytest.mark.unit

LEDGER = "docs/tracking/ROLLING_WORK_LEDGER.md"


def at(hour: float) -> str:
    moment = datetime(2026, 9, 13, tzinfo=UTC) + timedelta(hours=hour)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def now(hour: float) -> datetime:
    return datetime(2026, 9, 13, tzinfo=UTC) + timedelta(hours=hour)


# ---------------------------------------------------------------------------
# Transitions
# ---------------------------------------------------------------------------


def test_a_claim_makes_a_holder() -> None:
    text = f"{at(0)} | CLAIM | lane=lane-a | ticket=OMN-1 | doing the thing\n"
    index = ci.build_index(text, LEDGER, now=now(1))
    held = ci.holder(index, "OMN-1")
    assert held is not None
    assert held.lane == "lane-a"
    assert held.state == "held"
    assert held.claim_line == 1


def test_a_release_gives_the_ticket_up() -> None:
    text = (
        f"{at(0)} | CLAIM | lane=lane-a | ticket=OMN-1 | mine\n"
        f"{at(1)} | RELEASE | lane=lane-a | ticket=OMN-1 | done with it\n"
    )
    index = ci.build_index(text, LEDGER, now=now(2))
    assert ci.holder(index, "OMN-1") is None


def test_a_second_lane_may_claim_after_a_release() -> None:
    text = (
        f"{at(0)} | CLAIM | lane=lane-a | ticket=OMN-1 | mine\n"
        f"{at(1)} | RELEASE | lane=lane-a | ticket=OMN-1 | done\n"
        f"{at(2)} | CLAIM | lane=lane-b | ticket=OMN-1 | now mine\n"
    )
    held = ci.holder(ci.build_index(text, LEDGER, now=now(3)), "OMN-1")
    assert held is not None
    assert held.lane == "lane-b"


def test_a_holders_terminal_releases_the_ticket() -> None:
    """A lane that closed with a TERMINAL no longer holds its ticket.
    Read as activity, it kept the ticket `held` for the whole staleness window,
    so a lab-fill tick skipped candidates whose lanes had all closed."""
    text = (
        f"{at(0)} | CLAIM | lane=lane-a | ticket=OMN-1 | mine\n"
        f"{at(1)} | TERMINAL | lane=lane-a | ticket=OMN-1 | closes-CLAIM={at(0)} | outcome=done\n"
    )
    index = ci.build_index(text, LEDGER, now=now(2))
    assert ci.holder(index, "OMN-1") is None


@pytest.mark.parametrize("rolled", [False, True])
def test_claim_index_terminal_releases_closed_claim(rolled: bool) -> None:
    """closing a claim frees it immediately, preserving a live peer."""
    claims = (
        f"{at(0)} | CLAIM | lane=closed | ticket=OMN-1 | mine\n"
        f"{at(0.1)} | CLAIM | lane=peer | ticket=OMN-2 | theirs\n"
    )
    terminal = (
        f"{at(1)} | TERMINAL | lane=closed | tickets=OMN-1,OMN-2 | "
        f"closes-CLAIM={at(0)} | outcome=done\n"
    )
    sources = (
        [ci.Source("archive/claims.md", claims), ci.Source(LEDGER, terminal)]
        if rolled
        else [ci.Source(LEDGER, claims + terminal)]
    )
    before = ci.build_index_from_sources([ci.Source(LEDGER, claims)], now=now(1))
    peer = ci.holder(before, "OMN-2")
    assert ci.holder(before, "OMN-1") is not None
    assert peer is not None
    assert peer.state == "held"

    after = ci.build_index_from_sources(sources, now=now(1))
    assert ci.holder(after, "OMN-1") is None
    remaining = ci.holder(after, "OMN-2")
    assert remaining is not None
    assert remaining.state == "held"
    assert (
        remaining.lane,
        remaining.fence,
        remaining.claimed_at,
        remaining.last_activity_at,
    ) == (
        peer.lane,
        peer.fence,
        peer.claimed_at,
        peer.last_activity_at,
    )


def test_a_terminal_releases_when_it_names_no_or_another_claim_stamp() -> None:
    """The lane name is the identity. A TERMINAL from the holder releases even
    when its closes-CLAIM stamp is missing or does not match the live claim,
    because a lane that wrote a TERMINAL is finished with the ticket either way."""
    for cell in ("", f"closes-CLAIM={at(0.5)} | "):
        text = (
            f"{at(0)} | CLAIM | lane=lane-a | ticket=OMN-1 | mine\n"
            f"{at(1)} | TERMINAL | lane=lane-a | ticket=OMN-1 | {cell}outcome=done\n"
        )
        assert ci.holder(ci.build_index(text, LEDGER, now=now(2)), "OMN-1") is None, (
            cell
        )


def test_a_terminal_from_another_lane_does_not_release_the_holder() -> None:
    """Same rule as RELEASE: otherwise any lane takes a ticket in two rows."""
    text = (
        f"{at(0)} | CLAIM | lane=lane-a | ticket=OMN-1 | mine\n"
        f"{at(1)} | TERMINAL | lane=lane-b | ticket=OMN-1 | closes-CLAIM={at(0)} | outcome=done\n"
    )
    held = ci.holder(ci.build_index(text, LEDGER, now=now(2)), "OMN-1")
    assert held is not None
    assert held.lane == "lane-a"
    assert held.state == "held"


def test_a_terminal_with_disjoint_tickets_does_not_release() -> None:
    text = (
        f"{at(0)} | CLAIM | lane=lane-a | ticket=OMN-1 | mine\n"
        f"{at(1)} | TERMINAL | lane=lane-a | ticket=OMN-2 | closes-CLAIM={at(0)} | outcome=done\n"
    )
    held = ci.holder(ci.build_index(text, LEDGER, now=now(2)), "OMN-1")
    assert held is not None
    assert held.lane == "lane-a"


def test_a_terminal_naming_several_tickets_releases_each_the_lane_holds() -> None:
    text = (
        f"{at(0)} | CLAIM | lane=lane-a | ticket=OMN-1 | mine\n"
        f"{at(0.1)} | CLAIM | lane=lane-b | ticket=OMN-2 | theirs\n"
        f"{at(1)} | TERMINAL | lane=lane-a | tickets=OMN-1,OMN-2 | outcome=done\n"
    )
    index = ci.build_index(text, LEDGER, now=now(2))
    assert ci.holder(index, "OMN-1") is None
    held = ci.holder(index, "OMN-2")
    assert held is not None
    assert held.lane == "lane-b"


def test_a_claim_by_another_lane_after_the_terminal_takes_the_ticket_with_the_next_fence() -> (
    None
):
    text = (
        f"{at(0)} | CLAIM | lane=lane-a | ticket=OMN-1 | mine\n"
        f"{at(1)} | TERMINAL | lane=lane-a | ticket=OMN-1 | closes-CLAIM={at(0)} | outcome=done\n"
        f"{at(2)} | CLAIM | lane=lane-b | ticket=OMN-1 | now mine\n"
    )
    held = ci.holder(ci.build_index(text, LEDGER, now=now(3)), "OMN-1")
    assert held is not None
    assert held.lane == "lane-b"
    assert held.state == "held"
    assert held.fence == 2, (
        "the fence is monotonic across a release, per the design of record"
    )


def test_the_fence_survives_a_release_row_too() -> None:
    text = (
        f"{at(0)} | CLAIM | lane=lane-a | ticket=OMN-1 | mine\n"
        f"{at(1)} | RELEASE | lane=lane-a | ticket=OMN-1 | done\n"
        f"{at(2)} | CLAIM | lane=lane-b | ticket=OMN-1 | now mine\n"
    )
    held = ci.holder(ci.build_index(text, LEDGER, now=now(3)), "OMN-1")
    assert held is not None
    assert held.fence == 2


def test_a_terminal_after_a_stale_takeover_releases_the_new_holder_only() -> None:
    """A TERMINAL written by the displaced lane after a takeover must not
    release the lane that took the ticket over."""
    text = (
        f"{at(0)} | CLAIM | lane=lane-a | ticket=OMN-1 | mine\n"
        f"{at(14)} | RECLAIM | lane=lane-b | ticket=OMN-1 | stale={LEDGER}:1\n"
        f"{at(15)} | TERMINAL | lane=lane-a | ticket=OMN-1 | closes-CLAIM={at(0)} | "
        "outcome=failed\n"
    )
    held = ci.holder(ci.build_index(text, LEDGER, now=now(16)), "OMN-1")
    assert held is not None
    assert held.lane == "lane-b"


def test_handover_transfers_without_passing_through_unclaimed() -> None:
    """One row, no gap. Requiring the receiving lane to accept would leave a
    ticket held by nobody and claimable by nobody when the receiver has already
    stopped."""
    text = (
        f"{at(0)} | CLAIM | lane=lane-a | ticket=OMN-1 | mine\n"
        f"{at(1)} | HANDOVER | lane=lane-a | ticket=OMN-1 | to=lane-b | you take it\n"
    )
    held = ci.holder(ci.build_index(text, LEDGER, now=now(2)), "OMN-1")
    assert held is not None
    assert held.lane == "lane-b"
    assert held.state == "held"


def test_a_handover_naming_no_receiver_is_not_a_transfer() -> None:
    text = (
        f"{at(0)} | CLAIM | lane=lane-a | ticket=OMN-1 | mine\n"
        f"{at(1)} | HANDOVER | lane=lane-a | ticket=OMN-1 | you take it\n"
    )
    held = ci.holder(ci.build_index(text, LEDGER, now=now(2)), "OMN-1")
    assert held is not None
    assert held.lane == "lane-a"


def test_only_the_holder_can_release_or_hand_over() -> None:
    """Otherwise release is a way for any lane to take a ticket in two rows,
    which is the collision this is meant to make visible."""
    text = (
        f"{at(0)} | CLAIM | lane=lane-a | ticket=OMN-1 | mine\n"
        f"{at(1)} | RELEASE | lane=lane-b | ticket=OMN-1 | not mine to release\n"
        f"{at(2)} | HANDOVER | lane=lane-c | ticket=OMN-1 | to=lane-c | nor mine\n"
    )
    held = ci.holder(ci.build_index(text, LEDGER, now=now(3)), "OMN-1")
    assert held is not None
    assert held.lane == "lane-a"


# ---------------------------------------------------------------------------
# Two lanes claiming at once
# ---------------------------------------------------------------------------


def test_two_claims_on_one_ticket_resolve_to_one_holder() -> None:
    """Phase 5 AC4. The ledger append lock serialises the two writes, so the
    index sees them in order and the first wins. Both rows survive in the file,
    which is what makes the collision legible afterwards."""
    text = (
        f"{at(0)} | CLAIM | lane=lane-a | ticket=OMN-1 | mine\n"
        f"{at(0)} | CLAIM | lane=lane-b | ticket=OMN-1 | also mine\n"
    )
    index = ci.build_index(text, LEDGER, now=now(1))
    held = ci.holder(index, "OMN-1")
    assert held is not None
    assert held.lane == "lane-a"
    assert ci.refusal_for_claim(index, "OMN-1", "lane-b", now=now(1)) is not None


def test_the_holder_reclaiming_its_own_ticket_is_not_a_collision() -> None:
    text = f"{at(0)} | CLAIM | lane=lane-a | ticket=OMN-1 | mine\n"
    index = ci.build_index(text, LEDGER, now=now(1))
    assert ci.refusal_for_claim(index, "OMN-1", "lane-a", now=now(1)) is None


def test_a_claim_refusal_names_the_holder_and_the_row() -> None:
    text = f"{at(0)} | CLAIM | lane=lane-a | ticket=OMN-1 | mine\n"
    index = ci.build_index(text, LEDGER, now=now(1))
    reason = ci.refusal_for_claim(index, "OMN-1", "lane-b", now=now(1))
    assert reason is not None
    assert "lane-a" in reason
    assert f"{LEDGER}:1" in reason
    assert "RELEASE" in reason or "HANDOVER" in reason


# ---------------------------------------------------------------------------
# The fencing token
# ---------------------------------------------------------------------------


def test_the_fence_increments_on_taking_and_not_on_keeping() -> None:
    """The preemption criterion on this store. Without it a preempted lane
    is indistinguishable from the current holder and the refusal has nothing to
    fire on."""
    text = (
        f"{at(0)} | CLAIM | lane=lane-a | ticket=OMN-1 | mine\n"
        f"{at(1)} | RENEW | lane=lane-a | ticket=OMN-1 | still going\n"
        f"{at(2)} | HANDOVER | lane=lane-a | ticket=OMN-1 | to=lane-b | yours\n"
    )
    index = ci.build_index(text, LEDGER, now=now(3))
    held = ci.holder(index, "OMN-1")
    assert held is not None
    assert held.fence == 2, "claim took it (1), renew kept it, handover took it (2)"


def test_a_commit_carrying_a_stale_fence_is_refused() -> None:
    text = (
        f"{at(0)} | CLAIM | lane=lane-a | ticket=OMN-1 | mine\n"
        f"{at(20)} | RECLAIM | lane=lane-b | ticket=OMN-1 | "
        f"stale={LEDGER}:1 | last_activity={at(0)} | taking it over\n"
    )
    index = ci.build_index(text, LEDGER, now=now(21))
    held = ci.holder(index, "OMN-1")
    assert held is not None
    assert held.lane == "lane-b"
    assert held.fence == 2
    stale = ci.refusal_for_push(index, "OMN-1", "lane-a", fence=1, now=now(21))
    assert stale is not None
    assert "lane-b" in stale
    current = ci.refusal_for_push(index, "OMN-1", "lane-b", fence=2, now=now(21))
    assert current is None


# ---------------------------------------------------------------------------
# Renewal, and why it is not a heartbeat
# ---------------------------------------------------------------------------


def test_any_row_by_the_holder_on_the_ticket_renews_it() -> None:
    """A heartbeat somebody has to remember fails the same way a non-renewable
    lease does, one step later. Lanes already write progress rows."""
    text = (
        f"{at(0)} | CLAIM | lane=lane-a | ticket=OMN-1 | mine\n"
        f"{at(11)} | PROGRESS | lane=lane-a | ticket=OMN-1 | still working\n"
    )
    index = ci.build_index(text, LEDGER, now=now(20))
    held = ci.holder(index, "OMN-1")
    assert held is not None
    assert held.state == "held", "activity at hour 11 keeps it live at hour 20"


def test_a_row_by_another_lane_does_not_renew_the_holder() -> None:
    text = (
        f"{at(0)} | CLAIM | lane=lane-a | ticket=OMN-1 | mine\n"
        f"{at(11)} | PROGRESS | lane=lane-z | ticket=OMN-1 | commenting from outside\n"
    )
    index = ci.build_index(text, LEDGER, now=now(20))
    held = ci.holder(index, "OMN-1")
    assert held is not None
    assert held.state == "stale"


# ---------------------------------------------------------------------------
# Staleness and reclaim without an operator
# ---------------------------------------------------------------------------


def test_the_staleness_window_is_declared_and_measured() -> None:
    assert ci.STALENESS_HOURS == 12


def test_a_quiet_claim_goes_stale_and_is_reclaimable() -> None:
    text = f"{at(0)} | CLAIM | lane=lane-a | ticket=OMN-1 | mine\n"
    index = ci.build_index(text, LEDGER, now=now(13))
    held = ci.holder(index, "OMN-1")
    assert held is not None
    assert held.state == "stale"
    assert ci.refusal_for_claim(index, "OMN-1", "lane-b", now=now(13)) is None


def test_staleness_is_measured_from_activity_not_from_the_claim() -> None:
    """A long, active piece of work must never go stale. Measuring from the
    claim would reclaim exactly the lanes doing the most work."""
    rows = [f"{at(0)} | CLAIM | lane=lane-a | ticket=OMN-1 | mine"]
    rows += [
        f"{at(h)} | PROGRESS | lane=lane-a | ticket=OMN-1 | hour {h}"
        for h in range(1, 40)
    ]
    index = ci.build_index("\n".join(rows) + "\n", LEDGER, now=now(40))
    held = ci.holder(index, "OMN-1")
    assert held is not None
    assert held.state == "held"


def test_reclaim_must_cite_the_stale_row() -> None:
    """A reclaim that cites nothing is just a second claim. The citation is what
    makes the takeover resolvable by reading the file afterwards."""
    text = (
        f"{at(0)} | CLAIM | lane=lane-a | ticket=OMN-1 | mine\n"
        f"{at(20)} | RECLAIM | lane=lane-b | ticket=OMN-1 | taking it\n"
    )
    held = ci.holder(ci.build_index(text, LEDGER, now=now(21)), "OMN-1")
    assert held is not None
    assert held.lane == "lane-a"


def test_reclaim_is_refused_against_a_live_claim() -> None:
    text = (
        f"{at(0)} | CLAIM | lane=lane-a | ticket=OMN-1 | mine\n"
        f"{at(2)} | RECLAIM | lane=lane-b | ticket=OMN-1 | "
        f"stale={LEDGER}:1 | last_activity={at(0)} | not actually stale\n"
    )
    held = ci.holder(ci.build_index(text, LEDGER, now=now(3)), "OMN-1")
    assert held is not None
    assert held.lane == "lane-a"


# ---------------------------------------------------------------------------
# The push refusal the enforcement items consume
# ---------------------------------------------------------------------------


def test_pushing_to_a_ticket_held_by_another_lane_is_refused() -> None:
    text = f"{at(0)} | CLAIM | lane=lane-a | ticket=OMN-1 | mine\n"
    index = ci.build_index(text, LEDGER, now=now(1))
    reason = ci.refusal_for_push(index, "OMN-1", "lane-b", fence=1, now=now(1))
    assert reason is not None
    assert "lane-a" in reason
    assert f"{LEDGER}:1" in reason


def test_pushing_to_an_unclaimed_ticket_is_allowed() -> None:
    """Deliberate. The refusal fires on a ticket held by ANOTHER lane, never on
    the absence of a claim. Requiring a claim for every push makes a
    coordination mechanism into a tax on ordinary work, and a tax on ordinary
    work is what gets routed around."""
    index = ci.build_index("", LEDGER, now=now(1))
    assert ci.refusal_for_push(index, "OMN-1", "lane-b", fence=None, now=now(1)) is None


def test_pushing_to_a_stale_claim_is_allowed() -> None:
    text = f"{at(0)} | CLAIM | lane=lane-a | ticket=OMN-1 | mine\n"
    index = ci.build_index(text, LEDGER, now=now(13))
    assert (
        ci.refusal_for_push(index, "OMN-1", "lane-b", fence=None, now=now(13)) is None
    )


def test_ticket_from_branch_reads_the_branch_name() -> None:
    assert ci.ticket_from_branch("operator/omn-123-claim-index") == "OMN-123"
    assert ci.ticket_from_branch("operator/OMN-123-claim-index") == "OMN-123"
    assert ci.ticket_from_branch("dev") is None
    assert ci.ticket_from_branch("operator/no-ticket-here") is None


# ---------------------------------------------------------------------------
# The index is a cache, and is treated as one
# ---------------------------------------------------------------------------


def test_the_index_records_what_it_was_built_from(tmp_path: Path) -> None:
    text = f"{at(0)} | CLAIM | lane=lane-a | ticket=OMN-1 | mine\n"
    index = ci.build_index(text, LEDGER, now=now(1))
    assert index["source_line_count"] == 1
    assert index["source_digest"]
    assert index["version"] == ci.INDEX_VERSION


def test_a_stale_index_is_detected_against_the_ledger() -> None:
    text = f"{at(0)} | CLAIM | lane=lane-a | ticket=OMN-1 | mine\n"
    index = ci.build_index(text, LEDGER, now=now(1))
    assert not ci.index_is_stale(index, text)
    grown = text + f"{at(1)} | CLAIM | lane=lane-b | ticket=OMN-2 | mine\n"
    assert ci.index_is_stale(index, grown)


def test_an_edited_prefix_is_detected_even_at_the_same_length() -> None:
    """Line count alone would miss a rewritten row. The digest is why a
    disagreement between index and ledger is always resolved in the ledger
    favour rather than assumed away."""
    text = f"{at(0)} | CLAIM | lane=lane-a | ticket=OMN-1 | mine\n"
    index = ci.build_index(text, LEDGER, now=now(1))
    edited = f"{at(0)} | CLAIM | lane=lane-z | ticket=OMN-1 | mine\n"
    assert ci.index_is_stale(index, edited)


def test_save_and_load_round_trip(tmp_path: Path) -> None:
    text = f"{at(0)} | CLAIM | lane=lane-a | ticket=OMN-1 | mine\n"
    index = ci.build_index(text, LEDGER, now=now(1))
    path = tmp_path / "claim_index.json"
    ci.save_index(index, path)
    assert ci.load_index(path) == index


def test_an_unreadable_index_loads_as_nothing_rather_than_as_empty(
    tmp_path: Path,
) -> None:
    """An index that cannot be read must read as ABSENT, so the caller rebuilds.
    Reading it as an empty index would report every ticket unclaimed, which is
    a gate passing the case it exists to catch."""
    path = tmp_path / "claim_index.json"
    path.write_text("{not json", encoding="utf-8")
    assert ci.load_index(path) is None
    assert ci.load_index(tmp_path / "missing.json") is None


def test_a_wrong_version_index_loads_as_nothing(tmp_path: Path) -> None:
    text = f"{at(0)} | CLAIM | lane=lane-a | ticket=OMN-1 | mine\n"
    index = ci.build_index(text, LEDGER, now=now(1))
    index["version"] = ci.INDEX_VERSION + 99
    path = tmp_path / "claim_index.json"
    ci.save_index(index, path)
    assert ci.load_index(path) is None


def test_resolve_rebuilds_when_the_index_is_absent_or_behind(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.md"
    ledger.write_text(
        f"{at(0)} | CLAIM | lane=lane-a | ticket=OMN-1 | mine\n", encoding="utf-8"
    )
    path = tmp_path / "claim_index.json"
    first = ci.resolve_index(ledger, path, LEDGER, now=now(1))
    assert ci.holder(first, "OMN-1") is not None
    assert path.is_file(), "resolving writes the cache so the next hook run is cheap"

    with ledger.open("a", encoding="utf-8") as handle:
        handle.write(f"{at(1)} | CLAIM | lane=lane-b | ticket=OMN-2 | mine\n")
    second = ci.resolve_index(ledger, path, LEDGER, now=now(2))
    assert ci.holder(second, "OMN-2") is not None, (
        "a behind index is rebuilt, not trusted"
    )


# ---------------------------------------------------------------------------
# A representative ledger: parse holders and bound full-rebuild cost
# ---------------------------------------------------------------------------


@pytest.fixture
def representative_ledger(tmp_path: Path) -> Path:
    """Seven thousand ledger rows, independent of the retiring registry checkout.

    Keep the original holder/line checks and two-second rebuild bound runnable
    even when no live markdown ledger is present in this package repository.
    """
    ledger = tmp_path / "ROLLING_WORK_LEDGER.md"
    moment = now(1)
    _write_ledger(
        ledger,
        [
            f"{_stamp(moment)} | CLAIM | lane=lane-{number % 100} "
            f"| ticket=OMN-{number + 1} | representative claim"
            for number in range(7000)
        ],
    )
    return ledger


def test_the_live_ledger_builds_an_index_with_real_holders(
    representative_ledger: Path,
) -> None:
    ledger = representative_ledger
    text = ledger.read_text(encoding="utf-8")
    index = ci.build_index(text, LEDGER, now=datetime.now(UTC))
    assert index["tickets"], "the live ledger carries claim rows; the index found none"
    for ticket, record in index["tickets"].items():
        assert ticket.startswith("OMN-")
        assert record["lane"]
        assert 1 <= record["claim_line"] <= len(text.splitlines())
        assert record["state"] in {"held", "stale"}


def test_building_the_live_index_is_fast_enough_for_a_hook(
    representative_ledger: Path,
) -> None:
    """The index exists because a hook cannot read the whole ledger on every
    push. A full rebuild is the maintenance path, not the hot path, but it still
    has to be bounded -- a rebuild that takes seconds would make every cold hook
    run feel like a hang and get the hook removed."""
    import time

    ledger = representative_ledger
    text = ledger.read_text(encoding="utf-8")
    start = time.monotonic()
    ci.build_index(text, LEDGER, now=datetime.now(UTC))
    elapsed = time.monotonic() - start
    assert elapsed < 2.0, f"full rebuild took {elapsed:.2f}s"


# ---------------------------------------------------------------------------
# Atomic transitions, run in PARALLEL -- phase 5 AC4 names this explicitly
# ---------------------------------------------------------------------------


CLAIMANT = """
import sys
from datetime import datetime, timezone
from omnimarket.handlers import claim_index as ci
from pathlib import Path

ledger = Path(sys.argv[1])
lane = sys.argv[2]
barrier = Path(sys.argv[3])
row = f"{sys.argv[4]} | CLAIM | lane={lane} | ticket=OMN-1 | racing"

# Spin until the starter file appears, so the processes enter the critical
# section as close together as the operating system allows. Starting them
# sequentially would prove nothing about concurrency.
while not barrier.exists():
    pass

try:
    ci.append_transition(
        ledger,
        row,
        ledger_name="L",
        now=datetime(2026, 9, 13, tzinfo=timezone.utc),
        timeout=30.0,
    )
    print("ACQUIRED")
except ci.ClaimCollisionError as exc:
    print("REFUSED")
    print(str(exc).splitlines()[0])
"""


def test_two_lanes_claiming_in_parallel_resolve_to_one_holder(tmp_path: Path) -> None:
    """Phase 5 AC4, and the ticket is explicit that a sequential test does not
    satisfy it.

    Real concurrent processes, released together by a barrier file, each trying
    to claim the same ticket. Exactly one acquires and exactly one is refused,
    and the ledger carries exactly one claim row afterwards.

    The critical section covers the DECISION as well as the write. Locking only
    the write would let both processes read an unclaimed ledger, both decide
    they may claim, and both append -- two holders, no error anywhere, which is
    the collision this phase exists to remove.
    """

    script = tmp_path / "claimant.py"
    script.write_text(CLAIMANT, encoding="utf-8")
    ledger = tmp_path / "ledger.md"
    ledger.write_text("", encoding="utf-8")
    barrier = tmp_path / "go"

    procs = [
        subprocess.Popen(
            [
                sys.executable,
                str(script),
                str(ledger),
                lane,
                str(barrier),
                at(0),
            ],
            stdout=subprocess.PIPE,
            text=True,
        )
        for lane in ("lane-a", "lane-b")
    ]
    barrier.write_text("go", encoding="utf-8")
    outputs = [p.communicate()[0] for p in procs]

    acquired = [o for o in outputs if o.startswith("ACQUIRED")]
    refused = [o for o in outputs if o.startswith("REFUSED")]
    assert len(acquired) == 1, f"expected exactly one holder, got {outputs}"
    assert len(refused) == 1, f"expected exactly one refusal, got {outputs}"
    assert "already held by" in refused[0]

    body = ledger.read_text(encoding="utf-8")
    assert body.count("| CLAIM |") == 1, "the refused claim must not have been written"

    index = ci.build_index(body, LEDGER, now=now(1))
    held = ci.holder(index, "OMN-1")
    assert held is not None
    assert held.lane in {"lane-a", "lane-b"}
    assert held.fence == 1


def test_the_transition_lock_is_exclusive(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.md"
    ledger.write_text("", encoding="utf-8")
    with (
        ci.transition_lock(ledger, timeout=5.0),
        pytest.raises(TimeoutError),
        ci.transition_lock(ledger, timeout=0.2),
    ):
        pass


def test_the_lock_is_released_even_when_the_body_raises(tmp_path: Path) -> None:
    """A lock a failed transition keeps forever is worse than no lock: it stalls
    every later lane with no diagnosis."""
    ledger = tmp_path / "ledger.md"
    ledger.write_text("", encoding="utf-8")
    with (
        pytest.raises(ValueError, match="boom"),
        ci.transition_lock(ledger, timeout=5.0),
    ):
        raise ValueError("boom")
    with ci.transition_lock(ledger, timeout=1.0):
        pass


def test_append_transition_refuses_a_colliding_claim_and_writes_nothing(
    tmp_path: Path,
) -> None:
    ledger = tmp_path / "ledger.md"
    ledger.write_text(
        f"{at(0)} | CLAIM | lane=lane-a | ticket=OMN-1 | mine\n", encoding="utf-8"
    )
    before = ledger.read_text(encoding="utf-8")
    with pytest.raises(ci.ClaimCollisionError):
        ci.append_transition(
            ledger,
            f"{at(1)} | CLAIM | lane=lane-b | ticket=OMN-1 | also mine",
            ledger_name=LEDGER,
            now=now(1),
        )
    assert ledger.read_text(encoding="utf-8") == before


def test_append_transition_allows_a_non_colliding_claim_and_refreshes_the_index(
    tmp_path: Path,
) -> None:
    ledger = tmp_path / "ledger.md"
    ledger.write_text(
        f"{at(0)} | CLAIM | lane=lane-a | ticket=OMN-1 | mine\n", encoding="utf-8"
    )
    index_path = tmp_path / "index.json"
    index = ci.append_transition(
        ledger,
        f"{at(1)} | CLAIM | lane=lane-b | ticket=OMN-2 | mine",
        ledger_name=LEDGER,
        index_path=index_path,
        now=now(1),
    )
    assert ci.holder(index, "OMN-2") is not None
    assert index_path.is_file()
    assert not ci.index_is_stale(
        ci.load_index(index_path) or {}, ledger.read_text(encoding="utf-8")
    )


# ---------------------------------------------------------------------------
# the claim store is the live ledger AND the rolls it spilled into
#
# The ledger ROLLS. The roll MOVES rows out of the live file into
# `docs/tracking/archive/<stem>_<date>-split.md`. A resolution that reads only
# the live file loses every claim the last roll carried away, and a lost claim
# reads as an UNCLAIMED ticket -- which is clean. The gate passes exactly the
# case it exists to catch.
#
# The bound matters as much as the fix. Reading every archive would mean
# ~30 MB per push, which is how a hook gets removed rather than obeyed. Rolled
# rows are all older than the roll that moved them, so an archive whose roll
# date is before the staleness cutoff cannot hold an in-window row and is never
# opened.
# ---------------------------------------------------------------------------

SECTION_HEADING = "## §5 Action Log (append-only)"


def _stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_ledger(path: Path, rows: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "# ledger\n\n" + SECTION_HEADING + "\n\n" + "".join(f"{row}\n" for row in rows),
        encoding="utf-8",
    )


def test_an_archive_whose_roll_date_is_inside_the_window_is_read_and_named(
    tmp_path: Path,
) -> None:
    """AC1 and AC3 together: the in-window archive is a source, and the holder
    it yields is attributed to THAT file rather than to the live ledger."""
    moment = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
    ledger = tmp_path / "docs" / "tracking" / "ROLLING_WORK_LEDGER.md"
    archive = ledger.parent / "archive" / "ROLLING_WORK_LEDGER_2026-09-18-split.md"
    archive.parent.mkdir(parents=True)
    archive.write_text(
        f"## rolled\n\n{_stamp(moment - timedelta(hours=3))} | CLAIM | lane=alpha "
        "| ticket=OMN-1 | took it before the roll\n",
        encoding="utf-8",
    )
    _write_ledger(
        ledger,
        [
            f"{_stamp(moment - timedelta(hours=1))} | NOTE | lane=beta | ticket=OMN-2 | unrelated"
        ],
    )

    sources = ci.window_sources(ledger, LEDGER, now=moment)
    assert [source.name for source in sources] == [
        "docs/tracking/archive/ROLLING_WORK_LEDGER_2026-09-18-split.md",
        LEDGER,
    ], "the in-window archive is read, oldest first, with the live ledger last"

    index = ci.build_index_from_sources(sources, now=moment)
    held = ci.holder(index, "OMN-1")
    assert held is not None
    assert held.lane == "alpha"
    assert held.state == "held"
    assert (
        held.source == "docs/tracking/archive/ROLLING_WORK_LEDGER_2026-09-18-split.md"
    )


def test_an_archive_whose_roll_date_is_outside_the_window_is_never_opened(
    tmp_path: Path,
) -> None:
    """AC1's bound. Rolled rows are all older than the roll that moved them, so
    an archive dated before the cutoff cannot hold an in-window row. Proven by
    putting a row in it that WOULD resolve if the file were read."""
    moment = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
    ledger = tmp_path / "docs" / "tracking" / "ROLLING_WORK_LEDGER.md"
    stale = ledger.parent / "archive" / "ROLLING_WORK_LEDGER_2026-09-01-split.md"
    stale.parent.mkdir(parents=True)
    stale.write_text(
        f"## rolled\n\n{_stamp(moment - timedelta(hours=1))} | CLAIM | lane=ghost "
        "| ticket=OMN-9 | a row an out-of-window archive must not contribute\n",
        encoding="utf-8",
    )
    _write_ledger(ledger, [])

    sources = ci.window_sources(ledger, LEDGER, now=moment)
    assert [source.name for source in sources] == [LEDGER]
    assert ci.holder(ci.build_index_from_sources(sources, now=moment), "OMN-9") is None


def test_an_archive_with_no_date_in_its_name_is_bounded_by_its_mtime(
    tmp_path: Path,
) -> None:
    """AC1's other half. A file the roll did not name in the expected shape is
    still bounded -- by the only other fact available about it -- rather than
    read unconditionally or dropped unconditionally."""
    moment = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
    ledger = tmp_path / "docs" / "tracking" / "ROLLING_WORK_LEDGER.md"
    _write_ledger(ledger, [])
    archive_dir = ledger.parent / "archive"
    archive_dir.mkdir(parents=True)

    recent = archive_dir / "ROLLING_WORK_LEDGER_hand-split.md"
    recent.write_text("## rolled\n\n", encoding="utf-8")
    os.utime(recent, (moment.timestamp() - 3600, moment.timestamp() - 3600))

    old = archive_dir / "ROLLING_WORK_LEDGER_older-hand-split.md"
    old.write_text("## rolled\n\n", encoding="utf-8")
    os.utime(old, (moment.timestamp() - 86400, moment.timestamp() - 86400))

    names = [source.name for source in ci.window_sources(ledger, LEDGER, now=moment)]
    assert "docs/tracking/archive/ROLLING_WORK_LEDGER_hand-split.md" in names
    assert "docs/tracking/archive/ROLLING_WORK_LEDGER_older-hand-split.md" not in names


def test_the_live_ledger_is_replayed_last_so_a_release_after_a_roll_wins(
    tmp_path: Path,
) -> None:
    """Ordering is not cosmetic. Replayed in the wrong order, a RELEASE that
    followed a rolled CLAIM would be applied to nothing and the ticket would
    read as held by a lane that gave it up."""
    moment = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
    ledger = tmp_path / "docs" / "tracking" / "ROLLING_WORK_LEDGER.md"
    archive = ledger.parent / "archive" / "ROLLING_WORK_LEDGER_2026-09-18-split.md"
    archive.parent.mkdir(parents=True)
    archive.write_text(
        f"## rolled\n\n{_stamp(moment - timedelta(hours=4))} | CLAIM | lane=alpha "
        "| ticket=OMN-1 | took it\n",
        encoding="utf-8",
    )
    _write_ledger(
        ledger,
        [
            f"{_stamp(moment - timedelta(hours=2))} | RELEASE | lane=alpha | ticket=OMN-1 | done with it",
        ],
    )
    index = ci.build_index_from_sources(
        ci.window_sources(ledger, LEDGER, now=moment), now=moment
    )
    assert ci.holder(index, "OMN-1") is None


def test_a_refusal_for_a_rolled_claim_cites_the_archive_not_the_live_ledger(
    tmp_path: Path,
) -> None:
    """AC3. A citation the reader cannot follow is worse than none: the row is
    no longer at that line of the live ledger, and it is not in that file at
    all."""
    moment = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
    ledger = tmp_path / "docs" / "tracking" / "ROLLING_WORK_LEDGER.md"
    archive = ledger.parent / "archive" / "ROLLING_WORK_LEDGER_2026-09-18-split.md"
    archive.parent.mkdir(parents=True)
    archive.write_text(
        f"## rolled\n\n{_stamp(moment - timedelta(hours=3))} | CLAIM | lane=alpha "
        "| ticket=OMN-1 | took it before the roll\n",
        encoding="utf-8",
    )
    _write_ledger(ledger, [])
    index = ci.build_index_from_sources(
        ci.window_sources(ledger, LEDGER, now=moment), now=moment
    )
    reason = ci.refusal_for_push(index, "OMN-1", "beta", fence=None, now=moment)
    assert reason is not None
    assert "docs/tracking/archive/ROLLING_WORK_LEDGER_2026-09-18-split.md:3" in reason
    assert f"{LEDGER}:3" not in reason, (
        "the live ledger must not be cited for a rolled row"
    )
    held = ci.holder(index, "OMN-1")
    assert held is not None
    cited = archive.read_text(encoding="utf-8").splitlines()[held.claim_line - 1]
    assert "| CLAIM | lane=alpha" in cited, "the cited line resolves to the claim row"


def test_a_pointer_to_an_unreadable_in_window_archive_refuses(tmp_path: Path) -> None:
    """Fail closed, and this is the case that matters in continuous integration:
    the check fetches the store with a sparse checkout, and a pattern that misses
    the archive directory leaves a store that answers "unclaimed" for every
    rolled claim. The live ledger's own roll pointer says the file should be
    there, so its absence is a store that cannot be resolved, not an empty one.
    """
    moment = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
    ledger = tmp_path / "docs" / "tracking" / "ROLLING_WORK_LEDGER.md"
    ledger.parent.mkdir(parents=True)
    pointer = json.dumps(
        {
            "archive": "docs/tracking/archive/ROLLING_WORK_LEDGER_2026-09-18-split.md",
            "entries_rolled": 12,
            "rolled_at": _stamp(moment - timedelta(hours=2)),
        },
        sort_keys=True,
    )
    ledger.write_text(
        f"# ledger\n\n{SECTION_HEADING}\n\n<!-- ledger-roll: {pointer} -->\n\n",
        encoding="utf-8",
    )
    with pytest.raises(ci.ClaimStoreIncompleteError) as caught:
        ci.window_sources(ledger, LEDGER, now=moment)
    assert "ROLLING_WORK_LEDGER_2026-09-18-split.md" in str(caught.value)


# --- a roll, against a synthetic fixture ledger ---------------------------


def _rolled_fixture(tmp_path: Path, moment: datetime) -> tuple[Path, str]:
    """A ledger whose oldest claim sits in an archive, with the roll pointer in the live file.

    Returns the live ledger and the claim row the roll carried into the archive.
    """
    ledger = tmp_path / "docs" / "ledger.md"
    archive = (
        ledger.parent / "archive" / f"ledger_{moment.strftime('%Y-%m-%d')}-split.md"
    )
    claim_row = (
        f"{_stamp(moment - timedelta(hours=4))} | CLAIM | lane=alpha | ticket=OMN-1 "
        "| claimed four hours ago, well inside the twelve-hour window"
    )
    _write_ledger(archive, [claim_row])
    pointer = json.dumps(
        {
            "archive": f"docs/archive/{archive.name}",
            "entries_rolled": 1,
            "rolled_at": _stamp(moment - timedelta(hours=1)),
        },
        sort_keys=True,
    )
    _write_ledger(
        ledger,
        [
            f"<!-- ledger-roll: {pointer} -->",
            f"{_stamp(moment - timedelta(hours=3))} | STATUS | lane=beta | ticket=OMN-2 | filler",
        ],
    )
    assert claim_row not in ledger.read_text(encoding="utf-8")
    return ledger, claim_row


def test_a_claim_the_real_roll_moved_still_resolves_inside_its_window(
    tmp_path: Path,
) -> None:
    """AC2, and the RED of this ticket. The row is four hours old against a
    twelve-hour window, so the claim is live by every rule the index has -- and
    before this change the resolution answered `unclaimed`, because the roll had
    moved the row into a file nothing read."""
    moment = datetime.now(UTC)
    ledger, claim_row = _rolled_fixture(tmp_path, moment)

    live_only = ci.build_index(ledger.read_text(encoding="utf-8"), LEDGER, now=moment)
    assert ci.holder(live_only, "OMN-1") is None, (
        "the live ledger alone no longer carries the claim -- this is the defect, "
        "and the assertion that keeps this test honest about what it is proving"
    )

    index = ci.build_index_from_sources(
        ci.window_sources(ledger, LEDGER, now=moment), now=moment
    )
    held = ci.holder(index, "OMN-1")
    assert held is not None, f"the rolled claim did not resolve; row was: {claim_row}"
    assert held.lane == "alpha"
    assert held.state == "held"
    assert held.source.endswith("-split.md"), (
        f"the holder must be attributed to the archive it was read from, got {held.source}"
    )
    assert (
        ci.refusal_for_push(index, "OMN-1", "beta", fence=None, now=moment) is not None
    )


def test_resolve_index_reads_the_rolled_archive_and_records_every_source(
    tmp_path: Path,
) -> None:
    """The cached path must not be the live-only path in disguise: an index
    built before a roll is stale AFTER it, and a cache that cannot tell would
    serve the pre-roll answer until the live file happened to change."""
    moment = datetime.now(UTC)
    ledger, _ = _rolled_fixture(tmp_path, moment)
    index_path = tmp_path / "index.json"

    index = ci.resolve_index(ledger, index_path, LEDGER, now=moment)
    assert ci.holder(index, "OMN-1") is not None
    names = [entry["name"] for entry in index["sources"]]
    assert names[-1] == LEDGER, names
    assert len(names) == 2, names
    assert index_path.is_file()

    again = ci.resolve_index(ledger, index_path, LEDGER, now=moment)
    assert ci.holder(again, "OMN-1") is not None


@pytest.mark.parametrize("row_class", ["TERMINAL", "RELEASE"])
def test_the_terminal_release_fence_survives_a_source_boundary(row_class: str) -> None:
    sources = [
        ci.Source(
            "archive/previous.md",
            f"{at(0)} | CLAIM | lane=lane-a | ticket=OMN-1\n"
            f"{at(1)} | {row_class} | lane=lane-a | ticket=OMN-1\n",
        ),
        ci.Source(LEDGER, f"{at(2)} | CLAIM | lane=lane-b | ticket=OMN-1\n"),
    ]
    held = ci.holder(ci.build_index_from_sources(sources, now=now(3)), "OMN-1")
    assert held is not None
    assert held.lane == "lane-b"
    assert held.fence == 2


def test_a_pre_terminal_release_cache_is_rebuilt_on_an_unchanged_ledger(
    tmp_path: Path,
) -> None:
    text = (
        f"{at(0)} | CLAIM | lane=lane-a | ticket=OMN-1\n"
        f"{at(1)} | TERMINAL | lane=lane-a | ticket=OMN-1 | outcome=done\n"
    )
    ledger, cache = tmp_path / "ledger.md", tmp_path / "index.json"
    ledger.write_text(text, encoding="utf-8")
    old = ci.build_index(text, str(ledger), now=now(2))
    old["version"] = 1
    old["tickets"] = ci.build_index(
        text.splitlines()[0] + "\n", str(ledger), now=now(2)
    )["tickets"]
    ci.save_index(old, cache)
    rebuilt = ci.resolve_index(ledger, cache, str(ledger), now=now(2))
    assert ci.holder(rebuilt, "OMN-1") is None
    assert rebuilt["version"] == ci.INDEX_VERSION
    assert rebuilt["version"] != 1
