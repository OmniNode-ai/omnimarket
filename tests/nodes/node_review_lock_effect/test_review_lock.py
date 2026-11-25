# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC1: one review lock per (repo#pr, head), replaying the 2026-11-17 dispatch order of 6047."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from omnimarket.nodes.node_review_lock_effect.handlers.handler_review_lock import (
    HandlerReviewLock,
)
from omnimarket.nodes.node_review_lock_effect.handlers.ledger_rows import parse_row
from omnimarket.nodes.node_review_lock_effect.models.model_review_lock import (
    EnumLockOutcome,
    ModelReviewLockRequest,
    ModelStatusRow,
)

FIXTURES = Path(__file__).parent / "fixtures"

# (dispatch tag, head) in the order the 2026-11-17 reviews of omnibase_infra#6047 were dispatched.
DISPATCH_ORDER = [
    ("1139", "7ce37985d13c"),
    ("1225", "7ce37985d13c"),
    ("1243", "0b0a4b819f86"),
    ("1310", "0b0a4b819f86"),
    ("1340", "9bdbc60874d2"),
    ("1345", "9bdbc60874d2"),
    ("1448", "fca8c3ea6891"),
]


class FakeLedger:
    """An in-memory ledger: the reader sees what the writer appended, in order."""

    def __init__(self, lines: list[str] | None = None) -> None:
        self.rows: list[ModelStatusRow] = []
        self.lines: list[str] = []
        for line in lines or []:
            self.append(line)

    def append(self, row: str) -> None:
        parsed = parse_row(row)
        assert parsed is not None, row
        self.lines.append(row)
        self.rows.append(parsed)

    def status_rows(self, repo: str) -> list[ModelStatusRow]:
        return [r for r in self.rows if repo in r.cells.get("pr", "") + r.lane]


class BlindLedger(FakeLedger):
    """A ledger the reader never sees into: the control with no lock check."""

    def status_rows(self, repo: str) -> list[ModelStatusRow]:
        return []


def _clock(tick: list[int]) -> datetime:
    tick[0] += 1
    return datetime(2026, 11, 17, 11, 0, 0, tzinfo=UTC).replace(minute=tick[0])


def _replay(ledger: FakeLedger, *, write_verdicts: bool) -> list[EnumLockOutcome]:
    tick = [0]
    handler = HandlerReviewLock(ledger, ledger, lambda: _clock(tick))
    outcomes = []
    for tag, head in DISPATCH_ORDER:
        result = handler.handle(
            ModelReviewLockRequest(repo="omnibase_infra", pr=6047, head=head, tag=tag)
        )
        outcomes.append(result.outcome)
        if write_verdicts and result.outcome is EnumLockOutcome.ACQUIRED:
            ledger.append(
                f"2026-11-17T12:{int(tag) % 60:02d}:00Z | STATUS"
                f" | lane=fable-omnibase_infra-6047-{tag} | verdict=CHANGES-NEEDED | head={head}"
                " | pr=omnibase_infra#6047 | Fable review posted"
            )
    return outcomes


@pytest.mark.parametrize("write_verdicts", [False, True])
def test_6047_order_accepts_four_and_refuses_three(write_verdicts: bool) -> None:
    outcomes = _replay(FakeLedger(), write_verdicts=write_verdicts)
    accepted = [o for o in outcomes if o is EnumLockOutcome.ACQUIRED]
    assert len(accepted) == 4
    assert [i for i, o in enumerate(outcomes) if o is not EnumLockOutcome.ACQUIRED] == [
        1,
        3,
        5,
    ]
    expected = (
        EnumLockOutcome.REFUSED_VERDICT
        if write_verdicts
        else EnumLockOutcome.REFUSED_LOCK
    )
    assert {o for o in outcomes if o is not EnumLockOutcome.ACQUIRED} == {expected}


def test_control_without_the_lock_check_dispatches_seven() -> None:
    outcomes = _replay(BlindLedger(), write_verdicts=False)
    assert outcomes.count(EnumLockOutcome.ACQUIRED) == 7


def test_refusal_carries_the_first_locks_pointer_and_one_row_per_acquire() -> None:
    ledger = FakeLedger()
    tick = [0]
    handler = HandlerReviewLock(ledger, ledger, lambda: _clock(tick))

    def request(tag: str) -> ModelReviewLockRequest:
        return ModelReviewLockRequest(
            repo="omnibase_infra", pr=6047, head="7ce37985d13c0000", tag=tag
        )

    first = handler.handle(request("1139"))
    assert first.outcome is EnumLockOutcome.ACQUIRED
    assert first.exit_code == 0
    assert len(ledger.lines) == 1
    row = parse_row(ledger.lines[0])
    assert row is not None
    assert row.row_type == "STATUS"
    assert row.cells["lane"] == "reviewlock-omnibase_infra-6047-1139"
    assert row.cells["head"] == "7ce37985d13c"
    assert row.cells["pr"] == "omnibase_infra#6047"
    second = handler.handle(request("1225"))
    assert second.outcome is EnumLockOutcome.REFUSED_LOCK
    assert second.exit_code == 3
    assert second.pointer is not None
    assert second.pointer.pointer == f"ledger:{row.row_id}"
    assert second.pointer.lane == "reviewlock-omnibase_infra-6047-1139"
    assert len(ledger.lines) == 1, "a refusal writes no row"


def test_other_pull_request_or_repo_or_head_is_not_refused() -> None:
    ledger = FakeLedger()
    handler = HandlerReviewLock(ledger, ledger)
    base = {"repo": "omnibase_infra", "pr": 6047, "head": "7ce37985d13c", "tag": "1139"}
    assert (
        handler.handle(ModelReviewLockRequest(**base)).outcome
        is EnumLockOutcome.ACQUIRED
    )
    for change in ({"pr": 6048}, {"repo": "omnimarket"}, {"head": "0b0a4b819f86"}):
        other = handler.handle(
            ModelReviewLockRequest(**{**base, **change, "tag": "1225"})
        )
        assert other.outcome is EnumLockOutcome.ACQUIRED, change


def test_dry_run_writes_nothing_and_still_refuses() -> None:
    ledger = FakeLedger()
    handler = HandlerReviewLock(ledger, ledger)
    req = ModelReviewLockRequest(
        repo="omnimarket", pr=1, head="a" * 12, tag="0001", dry_run=True
    )
    assert handler.handle(req).outcome is EnumLockOutcome.ACQUIRED
    assert ledger.lines == []
    handler.handle(req.model_copy(update={"dry_run": False}))
    assert handler.handle(req).outcome is EnumLockOutcome.REFUSED_LOCK


def test_expired_lock_without_verdict_is_taken_again_only_with_a_ttl() -> None:
    ledger = FakeLedger()
    early = datetime(2026, 11, 17, 10, 0, 0, tzinfo=UTC)
    later = datetime(2026, 11, 17, 13, 0, 0, tzinfo=UTC)
    HandlerReviewLock(ledger, ledger, lambda: early).handle(
        ModelReviewLockRequest(repo="omnimarket", pr=1, head="a" * 12, tag="0001")
    )
    handler = HandlerReviewLock(ledger, ledger, lambda: later)
    never = ModelReviewLockRequest(repo="omnimarket", pr=1, head="a" * 12, tag="0002")
    assert handler.handle(never).outcome is EnumLockOutcome.REFUSED_LOCK
    ttl = never.model_copy(update={"lock_ttl_minutes": 120})
    assert handler.handle(ttl).outcome is EnumLockOutcome.ACQUIRED


def test_lost_race_is_refused_with_the_earlier_lock() -> None:
    """Two acquires both read an empty ledger; only the first lock row in ledger order wins."""

    class RacingLedger(FakeLedger):
        def __init__(self) -> None:
            super().__init__()
            self.reads = 0

        def status_rows(self, repo: str) -> list[ModelStatusRow]:
            self.reads += 1
            if self.reads == 1:
                return []
            return super().status_rows(repo)

    ledger = RacingLedger()
    # The rival's row lands between our read and our write.
    ledger.append(
        "2026-11-17T11:00:00Z | STATUS | lane=reviewlock-omnimarket-1-0001 | state=review-lock"
        " | pr=omnimarket#1 | head=" + "a" * 12 + " | rival"
    )
    result = HandlerReviewLock(ledger, ledger).handle(
        ModelReviewLockRequest(repo="omnimarket", pr=1, head="a" * 12, tag="0002")
    )
    assert result.outcome is EnumLockOutcome.REFUSED_LOCK
    assert result.pointer is not None
    assert result.pointer.lane == "reviewlock-omnimarket-1-0001"


def test_unreadable_ledger_is_undecided_not_acquired() -> None:
    class Broken:
        def status_rows(self, repo: str) -> list[ModelStatusRow]:
            raise FileNotFoundError("no ledger")

        def append(self, row: str) -> None:
            raise AssertionError("must not write")

    result = HandlerReviewLock(Broken(), Broken()).handle(
        ModelReviewLockRequest(repo="omnimarket", pr=1, head="a" * 12, tag="0001")
    )
    assert result.outcome is EnumLockOutcome.UNDECIDED
    assert result.exit_code == 4


def test_failed_write_is_undecided_not_acquired() -> None:
    class Refusing(FakeLedger):
        def append(self, row: str) -> None:
            raise OSError("ledger writer exit 1: refused")

    ledger = Refusing()
    result = HandlerReviewLock(ledger, ledger).handle(
        ModelReviewLockRequest(repo="omnimarket", pr=1, head="a" * 12, tag="0001")
    )
    assert result.outcome is EnumLockOutcome.UNDECIDED
    assert result.exit_code == 4


def test_real_6047_verdict_rows_refuse_every_reviewed_head() -> None:
    """The seven verdict rows as the live ledger holds them, parsed by the real row shapes."""
    ledger = FakeLedger((FIXTURES / "pr6047_verdict_rows.txt").read_text().splitlines())
    assert len(ledger.rows) == 7
    handler = HandlerReviewLock(ledger, ledger)
    first_lanes = {
        "7ce37985d13c": "fable-omnibase_infra-6047-1139",
        "0b0a4b819f86": "fable-omnibase_infra-6047-1243",
        "9bdbc60874d2": "fable-omnibase_infra-6047-1340",
        "fca8c3ea6891cc4a7b4becdeb8945b1bb308b853": "fable-omnibase_infra-6047-1448",
    }
    for head, lane in first_lanes.items():
        result = handler.handle(
            ModelReviewLockRequest(
                repo="omnibase_infra", pr=6047, head=head, tag="1500"
            )
        )
        assert result.outcome is EnumLockOutcome.REFUSED_VERDICT, head
        assert result.pointer is not None
        assert result.pointer.lane == lane
        assert result.pointer.verdict in {"CHANGES-NEEDED", "CHANGES_NEEDED"}
    fresh = handler.handle(
        ModelReviewLockRequest(
            repo="omnibase_infra", pr=6047, head="1" * 12, tag="1500"
        )
    )
    assert fresh.outcome is EnumLockOutcome.ACQUIRED


def test_verdict_pointer_is_the_verdict_file_when_it_exists(tmp_path: Path) -> None:
    ledger = FakeLedger((FIXTURES / "pr6047_verdict_rows.txt").read_text().splitlines())
    verdict = tmp_path / "verdict-omnibase_infra-6047-1139.md"
    verdict.write_text("CHANGES-NEEDED\n")
    result = HandlerReviewLock(ledger, ledger).handle(
        ModelReviewLockRequest(
            repo="omnibase_infra",
            pr=6047,
            head="7ce37985d13c",
            tag="1500",
            verdict_dir=str(tmp_path),
        )
    )
    assert result.pointer is not None
    assert result.pointer.pointer == str(verdict)
    assert result.pointer.ledger_row_id.endswith("fable-omnibase_infra-6047-1139")


def test_head_must_be_a_hex_sha() -> None:
    for bad in ("abc", "z" * 12, ""):
        with pytest.raises(ValueError, match="head"):
            ModelReviewLockRequest(repo="omnimarket", pr=1, head=bad, tag="0001")


def test_a_handler_built_with_no_ledger_ports_is_undecided_never_acquired() -> None:
    """The runtime builds the handler from injectable parameters alone (OMN-13551 boot guard)."""
    request = ModelReviewLockRequest(
        repo="omnibase_infra", pr=6047, head="7ce37985d13c", tag="1139"
    )
    result = HandlerReviewLock().handle(request)
    assert result.outcome is EnumLockOutcome.UNDECIDED
    assert "not wired" in result.detail
    # Positive control: the same request with a ledger wired acquires.
    ledger = FakeLedger()
    acquired = HandlerReviewLock(ledger, ledger).handle(request)
    assert acquired.outcome is EnumLockOutcome.ACQUIRED
