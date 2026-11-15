# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for the ledger reader: read_ledger, ModelLedgerRead, claim_maps_to_tree."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from omnimarket.handlers.handler_ledger_claims import (
    ModelLedgerRead,
    claim_maps_to_tree,
    claim_names_tree,
    claimed,
    live_claims,
    read_ledger,
)

NOW = datetime(2026, 1, 2, tzinfo=UTC)


def _write_ledger(tmp_path: Path, text: str, name: str = "LEDGER.md") -> Path:
    p = tmp_path / name
    p.write_text(text)
    return p


def test_fresh_ledger_with_claim_and_terminal_closes_the_claim(
    tmp_path: Path,
) -> None:
    """A CLAIM followed by a TERMINAL for lane L naming worktree path P:
    read.fresh is True, the claim row is in closed_claims and not in open_claims."""
    ledger = _write_ledger(
        tmp_path,
        "\n".join(
            [
                "2026-01-01T23:00:00Z | CLAIM | lane=L | ticket=OMN-1234 | /trees/OMN-1234/repo",
                "2026-01-01T23:40:00Z | TERMINAL | lane=L | friction=none",
            ]
        ),
    )
    result = read_ledger(
        ledger,
        NOW,
        max_bytes=10_000,
        max_age_hours=24.0,
        quiet_hours=6.0,
        lookback_days=0,
    )
    assert isinstance(result, ModelLedgerRead)
    assert result.fresh is True
    assert result.reason == ""
    # The CLAIM row is in closed_claims (lane L was closed by the TERMINAL)
    assert any("lane=L" in row and "CLAIM" in row for row in result.closed_claims)
    # The CLAIM row is NOT in open_claims
    assert not any("lane=L" in row and "CLAIM" in row for row in result.open_claims)


def test_empty_ledger_file_is_not_fresh(tmp_path: Path) -> None:
    """An empty ledger file: fresh False, reason 'empty'."""
    ledger = _write_ledger(tmp_path, "")
    result = read_ledger(
        ledger,
        NOW,
        max_bytes=10_000,
        max_age_hours=24.0,
        quiet_hours=6.0,
        lookback_days=0,
    )
    assert result.fresh is False
    assert result.reason == "empty"


def test_missing_ledger_file_is_not_fresh(tmp_path: Path) -> None:
    """A missing file: fresh False, reason 'missing'."""
    missing = tmp_path / "does_not_exist.md"
    result = read_ledger(
        missing,
        NOW,
        max_bytes=10_000,
        max_age_hours=24.0,
        quiet_hours=6.0,
        lookback_days=0,
    )
    assert result.fresh is False
    assert result.reason == "missing"


def test_unreadable_path_is_a_directory(tmp_path: Path) -> None:
    """An unreadable path (a directory): fresh False, reason 'unreadable'."""
    d = tmp_path / "a_dir"
    d.mkdir()
    result = read_ledger(
        d, NOW, max_bytes=10_000, max_age_hours=24.0, quiet_hours=6.0, lookback_days=0
    )
    assert result.fresh is False
    assert result.reason == "unreadable"


def test_file_over_max_bytes_is_too_large(tmp_path: Path) -> None:
    """A file over max_bytes: fresh False, reason 'too_large'."""
    big = "x" * 100
    ledger = _write_ledger(tmp_path, big)
    result = read_ledger(
        ledger, NOW, max_bytes=50, max_age_hours=24.0, quiet_hours=6.0, lookback_days=0
    )
    assert result.fresh is False
    assert result.reason == "too_large"


def test_frozen_ledger_is_not_fresh_but_recent_one_is(tmp_path: Path) -> None:
    """A frozen ledger (newest row 7 hours before NOW, max_age_hours 6): fresh False, reason 'frozen'.
    CONTROL: the same ledger with a row 1 hour old is fresh."""
    frozen_ledger = _write_ledger(
        tmp_path,
        "2026-01-01T17:00:00Z | STATUS | lane=L | ok",
    )
    result = read_ledger(
        frozen_ledger,
        NOW,
        max_bytes=10_000,
        max_age_hours=6.0,
        quiet_hours=6.0,
        lookback_days=0,
    )
    assert result.fresh is False
    assert result.reason == "frozen"

    # CONTROL: same ledger but with a row 1 hour old (within max_age_hours=6)
    recent_ledger = _write_ledger(
        tmp_path,
        "2026-01-01T23:00:00Z | STATUS | lane=L | ok",
        name="LEDGER_recent.md",
    )
    result2 = read_ledger(
        recent_ledger,
        NOW,
        max_bytes=10_000,
        max_age_hours=6.0,
        quiet_hours=6.0,
        lookback_days=0,
    )
    assert result2.fresh is True
    assert result2.reason == ""


def test_rolled_ledger_lookback_days_zero_vs_thirty(tmp_path: Path) -> None:
    """A rolled ledger: the live file holds only a recent STATUS row;
    the CLAIM and TERMINAL sit in archive/ROLLING_WORK_LEDGER_x.md next to it.
    With lookback_days 0 the claim is not seen;
    with lookback_days 30 it is in closed_claims."""
    # Live file: only a recent STATUS row
    live = _write_ledger(
        tmp_path,
        "2026-01-01T23:00:00Z | STATUS | lane=L | ok",
        name="LEDGER.md",
    )
    # Archive file next to it
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    archive = archive_dir / "ROLLING_WORK_LEDGER_1.md"
    archive.write_text(
        "\n".join(
            [
                "2025-12-01T10:00:00Z | CLAIM | lane=L | ticket=OMN-5555 | /trees/OMN-5555/repo",
                "2025-12-01T12:00:00Z | TERMINAL | lane=L | friction=none",
            ]
        )
    )
    # lookback_days=0: the archived claim is NOT seen
    result0 = read_ledger(
        live,
        NOW,
        max_bytes=10_000,
        max_age_hours=24.0,
        quiet_hours=6.0,
        lookback_days=0,
    )
    assert result0.fresh is True
    assert not any("OMN-5555" in row for row in result0.closed_claims)

    # lookback_days=30: the archived claim IS in closed_claims
    result30 = read_ledger(
        live,
        NOW,
        max_bytes=10_000,
        max_age_hours=24.0,
        quiet_hours=999.0,
        lookback_days=30,
    )
    assert result30.fresh is True
    assert any("OMN-5555" in row and "CLAIM" in row for row in result30.closed_claims)


def test_successor_claim_with_ticket_is_claimed(tmp_path: Path) -> None:
    """A live CLAIM of lane S with ticket=OMN-1234 and no TERMINAL;
    tree path root/OMN-1234-readings/repo: claimed(...) is True;
    a CONTROL tree root/OMN-9999-other/repo is False."""
    ledger = _write_ledger(
        tmp_path,
        "2026-01-01T23:00:00Z | CLAIM | lane=S | ticket=OMN-1234 | the successor lane, no tree named",
    )
    result = read_ledger(
        ledger,
        NOW,
        max_bytes=10_000,
        max_age_hours=24.0,
        quiet_hours=6.0,
        lookback_days=0,
    )
    assert result.fresh is True
    # The claim is open and live (no TERMINAL)
    assert any("OMN-1234" in row for row in result.live_claims)

    root = Path("/trees")
    # The tree path matching the ticket is claimed
    # The row names no path and no directory: only the ticket id in OMN-1234-readings maps it.
    assert claimed(root / "OMN-1234-readings" / "repo", root, result.live_claims)
    assert not claim_names_tree(
        root / "OMN-1234-readings" / "repo", root, result.live_claims[0]
    )
    # A different ticket is NOT claimed
    assert not claimed(root / "OMN-9999-other" / "repo", root, result.live_claims)


def test_quiet_open_claim_is_in_open_claims_but_not_live_claims(
    tmp_path: Path,
) -> None:
    """A quiet open claim (lane's last row older than quiet_hours)
    is in open_claims but not in live_claims."""
    # A CLAIM 10 hours ago, no TERMINAL; quiet_hours=6 means it is quiet
    ledger = _write_ledger(
        tmp_path,
        "2026-01-01T13:00:00Z | CLAIM | lane=quiet | ticket=OMN-7777 | /trees/OMN-7777/repo",
    )
    result = read_ledger(
        ledger,
        NOW,
        max_bytes=10_000,
        max_age_hours=24.0,
        quiet_hours=6.0,
        lookback_days=0,
    )
    assert result.fresh is True
    # The claim is in open_claims (no TERMINAL closed it)
    assert any("lane=quiet" in row and "CLAIM" in row for row in result.open_claims)

    # The quiet lane's claim is not live (its last row is 10 hours old, over quiet_hours 6) ...
    assert result.live_claims == ()
    assert live_claims(ledger.read_text(), NOW, quiet_hours=6.0) == ()
    # ... and CONTROL: the same lane with a heartbeat row 1 hour old is live.
    ledger.write_text(
        ledger.read_text()
        + "\n2026-01-01T23:00:00Z | STATUS | lane=quiet | still working\n"
    )
    live = read_ledger(
        ledger,
        NOW,
        max_bytes=10_000,
        max_age_hours=24.0,
        quiet_hours=6.0,
        lookback_days=0,
    )
    assert any("lane=quiet" in row for row in live.live_claims)


def test_claim_maps_to_tree_matches_path_or_ticket(tmp_path: Path) -> None:
    """claim_maps_to_tree returns True when the row names the path or its ticket directory."""
    root = Path("/trees")
    path = root / "OMN-1234" / "repo"
    row = (
        "2026-01-01T23:00:00Z | CLAIM | lane=S | ticket=OMN-1234 | /trees/OMN-1234/repo"
    )
    assert claim_maps_to_tree(path, root, row) is True
    # A different ticket does not match
    other = root / "OMN-9999" / "repo"
    assert claim_maps_to_tree(other, root, row) is False


def test_a_ledger_stamped_in_the_future_is_not_fresh(tmp_path: Path) -> None:
    """A writer with a skewed clock would look fresh forever: a stamp over an hour ahead is unread."""
    ledger = _write_ledger(tmp_path, "2026-01-02T05:00:00Z | STATUS | lane=L | ok")
    result = read_ledger(
        ledger,
        NOW,
        max_bytes=10_000,
        max_age_hours=6.0,
        quiet_hours=6.0,
        lookback_days=0,
    )
    assert (result.fresh, result.reason) == (False, "clock_skew")
