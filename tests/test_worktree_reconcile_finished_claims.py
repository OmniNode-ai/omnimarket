# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""owner_finished counts only a TERMINAL that ends the work on the tree.

A TERMINAL with outcome partial and the worktree kept, or a RELEASE of a
surface lease, closes the claim (it lands in closed_claims) but does not end
the work, so owner_finished stays False for the tree it names. A TERMINAL
whose worktree cell is removed, pinned-removed or released, or whose outcome
is done/merged/handed-off with no kept worktree, ends the work.

Every rule is paired with a CONTROL that differs in exactly one fact and
gives the opposite answer, so removing the rule makes the control fail.
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from omnimarket.handlers.rules_ledger_claims import (
    ModelLedgerRead,
    ends_work,
    fold_ledger_lines,
    owner_finished,
    read_ledger,
)

pytestmark = pytest.mark.unit
NOW = datetime(2026, 1, 10, 6, 30, tzinfo=UTC)


def _read(tmp_path: Path, text: str) -> ModelLedgerRead:
    ledger = tmp_path / "ledger"
    ledger.write_text(text)
    return read_ledger(
        ledger,
        NOW,
        max_bytes=10_000,
        max_age_hours=24.0,
        quiet_hours=6.0,
        lookback_days=0,
    )


def _owner_finished(tmp_path: Path, text: str, path: str) -> bool:
    return owner_finished(Path(path), Path("/"), _read(tmp_path, text))


def test_partial_kept_is_closed_but_not_finished(tmp_path: Path) -> None:
    """(a) A partial TERMINAL that keeps the worktree closes the claim but does not end the work."""
    text = (
        "2026-01-10T00:00:00Z | CLAIM | lane=a | ticket=OMN-1 | worktree=omni_worktrees/OMN-1/repo\n"
        "2026-01-10T01:00:00Z | TERMINAL | lane=a | ticket=OMN-1 | outcome=partial | worktree=kept:/p:reason\n"
    )
    read = _read(tmp_path, text)
    assert read.fresh
    assert any("OMN-1" in row for row in read.closed_claims)
    assert not any("OMN-1" in row for row in read.finished_claims)
    assert _owner_finished(tmp_path, text, "omni_worktrees/OMN-1/repo") is False
    # CONTROL: the same claim closed by a removed worktree ends the work.
    text2 = (
        "2026-01-10T00:00:00Z | CLAIM | lane=a | ticket=OMN-1 | worktree=omni_worktrees/OMN-1/repo\n"
        "2026-01-10T01:00:00Z | TERMINAL | lane=a | ticket=OMN-1 | outcome=partial | worktree=removed:/p\n"
    )
    assert any("OMN-1" in row for row in _read(tmp_path, text2).finished_claims)
    assert _owner_finished(tmp_path, text2, "omni_worktrees/OMN-1/repo") is True


def test_partial_removed_is_finished(tmp_path: Path) -> None:
    """(b) A partial TERMINAL that removes the worktree ends the work."""
    text = (
        "2026-01-10T00:00:00Z | CLAIM | lane=a | ticket=OMN-1 | worktree=omni_worktrees/OMN-1/repo\n"
        "2026-01-10T01:00:00Z | TERMINAL | lane=a | ticket=OMN-1 | outcome=partial | worktree=removed:/p\n"
    )
    read = _read(tmp_path, text)
    assert any("OMN-1" in row for row in read.closed_claims)
    assert any("OMN-1" in row for row in read.finished_claims)
    assert _owner_finished(tmp_path, text, "omni_worktrees/OMN-1/repo") is True
    # CONTROL: the same claim with a kept worktree does not end the work.
    text2 = (
        "2026-01-10T00:00:00Z | CLAIM | lane=a | ticket=OMN-1 | worktree=omni_worktrees/OMN-1/repo\n"
        "2026-01-10T01:00:00Z | TERMINAL | lane=a | ticket=OMN-1 | outcome=partial | worktree=kept:/p:x\n"
    )
    assert not any("OMN-1" in row for row in _read(tmp_path, text2).finished_claims)
    assert _owner_finished(tmp_path, text2, "omni_worktrees/OMN-1/repo") is False


def test_handed_off_without_worktree_is_finished(tmp_path: Path) -> None:
    """(c) A handed-off TERMINAL with no worktree cell ends the work."""
    text = (
        "2026-01-10T00:00:00Z | CLAIM | lane=a | ticket=OMN-1 | worktree=omni_worktrees/OMN-1/repo\n"
        "2026-01-10T01:00:00Z | TERMINAL | lane=a | ticket=OMN-1 | outcome=handed-off\n"
    )
    read = _read(tmp_path, text)
    assert any("OMN-1" in row for row in read.closed_claims)
    assert any("OMN-1" in row for row in read.finished_claims)
    assert _owner_finished(tmp_path, text, "omni_worktrees/OMN-1/repo") is True
    # CONTROL: the same handed-off outcome with a kept worktree does not end the work.
    text2 = (
        "2026-01-10T00:00:00Z | CLAIM | lane=a | ticket=OMN-1 | worktree=omni_worktrees/OMN-1/repo\n"
        "2026-01-10T01:00:00Z | TERMINAL | lane=a | ticket=OMN-1 | outcome=handed-off | worktree=kept:/p:x\n"
    )
    assert not any("OMN-1" in row for row in _read(tmp_path, text2).finished_claims)
    assert _owner_finished(tmp_path, text2, "omni_worktrees/OMN-1/repo") is False


def test_handed_off_kept_is_not_finished(tmp_path: Path) -> None:
    """(d) A handed-off TERMINAL that keeps the worktree does not end the work."""
    text = (
        "2026-01-10T00:00:00Z | CLAIM | lane=a | ticket=OMN-1 | worktree=omni_worktrees/OMN-1/repo\n"
        "2026-01-10T01:00:00Z | TERMINAL | lane=a | ticket=OMN-1 | outcome=handed-off | worktree=kept:/p:x\n"
    )
    read = _read(tmp_path, text)
    assert any("OMN-1" in row for row in read.closed_claims)
    assert not any("OMN-1" in row for row in read.finished_claims)
    assert _owner_finished(tmp_path, text, "omni_worktrees/OMN-1/repo") is False
    # CONTROL: the same handed-off outcome with a removed worktree ends the work.
    text2 = (
        "2026-01-10T00:00:00Z | CLAIM | lane=a | ticket=OMN-1 | worktree=omni_worktrees/OMN-1/repo\n"
        "2026-01-10T01:00:00Z | TERMINAL | lane=a | ticket=OMN-1 | outcome=handed-off | worktree=removed:/p\n"
    )
    assert any("OMN-1" in row for row in _read(tmp_path, text2).finished_claims)
    assert _owner_finished(tmp_path, text2, "omni_worktrees/OMN-1/repo") is True


def test_release_closes_but_does_not_end(tmp_path: Path) -> None:
    """(e) A RELEASE of a surface lease closes the claim but does not end the work."""
    text = (
        "2026-01-10T00:00:00Z | CLAIM | lane=a | ticket=OMN-1 | worktree=omni_worktrees/OMN-1/repo\n"
        "2026-01-10T01:00:00Z | RELEASE | lane=a | surface=202-runtime | result=PASS\n"
    )
    read = _read(tmp_path, text)
    assert any("OMN-1" in row for row in read.closed_claims)
    assert not any("OMN-1" in row for row in read.finished_claims)
    assert _owner_finished(tmp_path, text, "omni_worktrees/OMN-1/repo") is False
    # CONTROL: a TERMINAL that removes the worktree ends the work.
    text2 = (
        "2026-01-10T00:00:00Z | CLAIM | lane=a | ticket=OMN-1 | worktree=omni_worktrees/OMN-1/repo\n"
        "2026-01-10T01:00:00Z | TERMINAL | lane=a | ticket=OMN-1 | outcome=done | worktree=removed:/p\n"
    )
    assert any("OMN-1" in row for row in _read(tmp_path, text2).finished_claims)
    assert _owner_finished(tmp_path, text2, "omni_worktrees/OMN-1/repo") is True


def test_pinned_removed_is_finished(tmp_path: Path) -> None:
    """(f) A pinned-removed worktree cell ends the work."""
    text = (
        "2026-01-10T00:00:00Z | CLAIM | lane=a | ticket=OMN-1 | worktree=omni_worktrees/OMN-1/repo\n"
        "2026-01-10T01:00:00Z | TERMINAL | lane=a | ticket=OMN-1 | outcome=done | worktree=pinned-removed:/p@/archive.tgz\n"
    )
    read = _read(tmp_path, text)
    assert any("OMN-1" in row for row in read.closed_claims)
    assert any("OMN-1" in row for row in read.finished_claims)
    assert _owner_finished(tmp_path, text, "omni_worktrees/OMN-1/repo") is True
    # CONTROL: the same pinned-removed cell with a kept outcome does not end the work.
    text2 = (
        "2026-01-10T00:00:00Z | CLAIM | lane=a | ticket=OMN-1 | worktree=omni_worktrees/OMN-1/repo\n"
        "2026-01-10T01:00:00Z | TERMINAL | lane=a | ticket=OMN-1 | outcome=partial | worktree=kept:/p:x\n"
    )
    assert not any("OMN-1" in row for row in _read(tmp_path, text2).finished_claims)
    assert _owner_finished(tmp_path, text2, "omni_worktrees/OMN-1/repo") is False


def test_ends_work_truth_table() -> None:
    """(g) ends_work is True only for a TERMINAL that ends the work on the tree."""
    # Ending outcomes with no kept worktree.
    assert (
        ends_work(
            "2026-01-10T00:00:00Z | TERMINAL | lane=a | ticket=OMN-1 | outcome=done"
        )
        is True
    )
    assert (
        ends_work(
            "2026-01-10T00:00:00Z | TERMINAL | lane=a | ticket=OMN-1 | outcome=merged"
        )
        is True
    )
    assert (
        ends_work(
            "2026-01-10T00:00:00Z | TERMINAL | lane=a | ticket=OMN-1 | outcome=handed-off"
        )
        is True
    )
    # Ending outcomes with a removed or pinned-removed worktree.
    assert (
        ends_work(
            "2026-01-10T00:00:00Z | TERMINAL | lane=a | ticket=OMN-1 | outcome=partial | worktree=removed:/p"
        )
        is True
    )
    assert (
        ends_work(
            "2026-01-10T00:00:00Z | TERMINAL | lane=a | ticket=OMN-1 | outcome=partial | worktree=pinned-removed:/p@/archive.tgz"
        )
        is True
    )
    assert (
        ends_work(
            "2026-01-10T00:00:00Z | TERMINAL | lane=a | ticket=OMN-1 | outcome=partial | worktree=released"
        )
        is True
    )
    assert (
        ends_work(
            "2026-01-10T00:00:00Z | TERMINAL | lane=a | ticket=OMN-1 | outcome=partial | worktree=released:/p"
        )
        is True
    )
    # Kept worktree never ends the work, whatever the outcome.
    assert (
        ends_work(
            "2026-01-10T00:00:00Z | TERMINAL | lane=a | ticket=OMN-1 | outcome=done | worktree=kept:/p:x"
        )
        is False
    )
    assert (
        ends_work(
            "2026-01-10T00:00:00Z | TERMINAL | lane=a | ticket=OMN-1 | outcome=partial | worktree=kept:/p:x"
        )
        is False
    )
    # Non-ending outcomes with no kept worktree.
    assert (
        ends_work(
            "2026-01-10T00:00:00Z | TERMINAL | lane=a | ticket=OMN-1 | outcome=partial"
        )
        is False
    )
    assert (
        ends_work(
            "2026-01-10T00:00:00Z | TERMINAL | lane=a | ticket=OMN-1 | outcome=blocked"
        )
        is False
    )
    assert (
        ends_work(
            "2026-01-10T00:00:00Z | TERMINAL | lane=a | ticket=OMN-1 | outcome=failed"
        )
        is False
    )
    assert ends_work("2026-01-10T00:00:00Z | TERMINAL | lane=a | ticket=OMN-1") is False
    # A RELEASE row is always False.
    assert (
        ends_work(
            "2026-01-10T00:00:00Z | RELEASE | lane=a | surface=202-runtime | result=PASS"
        )
        is False
    )
    # A CLAIM row is False.
    assert (
        ends_work(
            "2026-01-10T00:00:00Z | CLAIM | lane=a | ticket=OMN-1 | worktree=omni_worktrees/OMN-1/repo"
        )
        is False
    )
    # A STATUS row is False.
    assert ends_work("2026-01-10T00:00:00Z | STATUS | lane=a | working") is False


def test_fold_ledger_lines_matches_read_ledger(tmp_path: Path) -> None:
    """(h) fold_ledger_lines gives the same open/closed/finished sets as read_ledger."""
    text = (
        "2026-01-10T00:00:00Z | CLAIM | lane=a | ticket=OMN-1 | worktree=omni_worktrees/OMN-1/repo\n"
        "2026-01-10T01:00:00Z | STATUS | lane=a | busy\n"
        "2026-01-10T02:00:00Z | CLAIM | lane=b | ticket=OMN-2 | worktree=omni_worktrees/OMN-2/repo\n"
        "2026-01-10T03:00:00Z | TERMINAL | lane=b | ticket=OMN-2 | outcome=done | worktree=removed:/p\n"
        "2026-01-10T04:00:00Z | CLAIM | lane=c | ticket=OMN-3 | worktree=omni_worktrees/OMN-3/repo\n"
        "2026-01-10T05:00:00Z | TERMINAL | lane=c | ticket=OMN-3 | outcome=partial | worktree=kept:/p:x\n"
        "2026-01-10T06:00:00Z | TERMINAL | lane=a | ticket=OMN-1 | outcome=done | worktree=removed:/p\n"
    )
    folded = fold_ledger_lines(text.splitlines(), NOW, quiet_hours=6.0)
    read = _read(tmp_path, text)
    assert folded.fresh is True
    assert tuple(folded.open_claims) == tuple(read.open_claims)
    assert tuple(folded.closed_claims) == tuple(read.closed_claims)
    assert tuple(folded.finished_claims) == tuple(read.finished_claims)
    assert tuple(folded.live_claims) == tuple(read.live_claims)
    # lane a's claim is closed by its own TERMINAL (removed worktree -> finished);
    # lane c's claim is closed by its own TERMINAL (kept worktree -> not finished).
    assert any("OMN-1" in row and "CLAIM" in row for row in folded.closed_claims)
    assert not any("OMN-1" in row and "CLAIM" in row for row in folded.open_claims)
    assert any("OMN-3" in row and "CLAIM" in row for row in folded.closed_claims)
    assert not any("OMN-3" in row and "CLAIM" in row for row in folded.open_claims)
    # lane a's and lane b's claims are finished (removed worktree); lane c's is not (kept worktree).
    assert any("OMN-1" in row and "CLAIM" in row for row in folded.finished_claims)
    assert any("OMN-2" in row and "CLAIM" in row for row in folded.finished_claims)
    assert not any("OMN-3" in row and "CLAIM" in row for row in folded.finished_claims)
    # CONTROL: a lane with only an old CLAIM and no heartbeat is not live, while a
    # lane with a heartbeat is live.
    text2 = (
        "2026-01-10T00:00:00Z | CLAIM | lane=a | ticket=OMN-1 | worktree=omni_worktrees/OMN-1/repo\n"
        "2026-01-10T01:00:00Z | STATUS | lane=a | busy\n"
        "2026-01-09T20:00:00Z | CLAIM | lane=b | ticket=OMN-2 | worktree=omni_worktrees/OMN-2/repo\n"
    )
    folded2 = fold_ledger_lines(text2.splitlines(), NOW, quiet_hours=6.0)
    assert any("lane=a" in row for row in folded2.live_claims)
    assert not any("lane=b" in row for row in folded2.live_claims)
