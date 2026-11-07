# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The guard replay re-judges a recorded week of rows and reports any row whose verdict changed (OMN-22915, L11).

Golden chain: a week of rows, each recorded with the verdict the live judge gave it, replays with an empty
difference list. Error chain: a recorded verdict the guards no longer give is listed with its position,
row and both verdicts. Failing control: a deliberately altered guard makes the same week's list non-empty.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from omnimarket.nodes.node_work_ledger_reducer_compute.logic.work_ledger_guard_replay import (
    HandlerWorkLedgerGuardReplay,
)
from omnimarket.nodes.node_work_ledger_reducer_compute.logic.work_ledger_judge import (
    judge_row,
)
from omnimarket.nodes.node_work_ledger_reducer_compute.models.model_work_ledger_judge import (
    EnumJudgeVerdict,
    ModelGuardReplayRequest,
    ModelGuardReplayResult,
    ModelJudgeVerdict,
    ModelRecordedRow,
)

pytestmark = pytest.mark.unit

LEDGER = "ROLLING_WORK_LEDGER.md"
WEEK_START = datetime(2026, 9, 21, 9, 0, tzinfo=UTC)
ACCEPT = ModelJudgeVerdict(verdict=EnumJudgeVerdict.ACCEPT)


def _stamp(at: datetime) -> str:
    return at.strftime("%Y-%m-%dT%H:%M:%SZ")


def _week() -> list[ModelRecordedRow]:
    """Seven days, three rows a day: a hold, its release, a status; one id reuse refused on day four."""
    entries: list[ModelRecordedRow] = []
    for day in range(7):
        at = WEEK_START + timedelta(days=day)
        hold_id = f"{_stamp(at)}-beta"
        release_at = at + timedelta(hours=2)
        rows = [
            (
                at,
                f"{_stamp(at)} | HOLD | lane=beta | id={hold_id} | surface=lab-dev | "
                f"until={_stamp(at + timedelta(hours=6))} | reserved",
            ),
            (
                release_at,
                f"{_stamp(release_at)} | RELEASE | lane=beta | re={hold_id} | surface=lab-dev | "
                "result=PASS | restored=yes | done",
            ),
            (
                at + timedelta(hours=3),
                f"{_stamp(at + timedelta(hours=3))} | STATUS | lane=alpha | ticket=OMN-1 | progress",
            ),
        ]
        for when, row in rows:
            entries.append(
                ModelRecordedRow(
                    row_ref=f"d{day}-{len(entries)}", row=row, recorded=ACCEPT, at=when
                )
            )
        if day == 3:
            reuse = (
                f"{_stamp(at + timedelta(hours=4))} | HOLD | lane=beta | id={hold_id} | "
                f"surface=lab-x | until={_stamp(at + timedelta(hours=8))} | again"
            )
            refusal = judge_row(reuse, "\n".join(e.row for e in entries), LEDGER)
            assert refusal.verdict is EnumJudgeVerdict.REFUSE
            entries.append(
                ModelRecordedRow(
                    row_ref=f"d{day}-reuse",
                    row=reuse,
                    recorded=refusal,
                    at=at + timedelta(hours=4),
                )
            )
    return entries


def _replay(
    entries: list[ModelRecordedRow], handler: HandlerWorkLedgerGuardReplay | None = None
) -> ModelGuardReplayResult:
    return (handler or HandlerWorkLedgerGuardReplay()).handle(
        ModelGuardReplayRequest(ledger_name=LEDGER, entries=tuple(entries))
    )


def test_golden_a_recorded_week_replays_with_no_difference() -> None:
    entries = _week()
    result = _replay(entries)
    assert result.judged == len(entries) == 22
    assert result.differences == ()
    assert result.equivalent is True
    assert result.window_start == WEEK_START
    assert result.window_end == WEEK_START + timedelta(days=6, hours=3)


def test_error_a_recorded_verdict_the_guards_no_longer_give_is_listed() -> None:
    entries = _week()
    # The live judge once accepted the id reuse; the guards refuse it, so the verdict changed.
    reuse_at = next(i for i, e in enumerate(entries) if e.row_ref == "d3-reuse")
    entries[reuse_at] = entries[reuse_at].model_copy(update={"recorded": ACCEPT})
    result = _replay(entries)
    assert result.equivalent is False
    assert [d.position for d in result.differences] == [reuse_at + 1]
    difference = result.differences[0]
    assert difference.row_ref == "d3-reuse"
    assert difference.recorded == ACCEPT
    assert difference.replayed.verdict is EnumJudgeVerdict.REFUSE
    assert (
        difference.difference
        == f"recorded accepted; replayed refused under {difference.replayed.rule}"
    )


def test_a_refusal_under_a_different_rule_is_listed() -> None:
    entries = _week()
    reuse_at = next(i for i, e in enumerate(entries) if e.row_ref == "d3-reuse")
    drifted = ModelJudgeVerdict(
        verdict=EnumJudgeVerdict.REFUSE, rule="OMN-1 an older rule", reason="r"
    )
    entries[reuse_at] = entries[reuse_at].model_copy(update={"recorded": drifted})
    result = _replay(entries)
    assert [d.row_ref for d in result.differences] == ["d3-reuse"]
    assert result.differences[0].difference.startswith(
        "both refused; recorded rule OMN-1 an older rule"
    )


def test_failing_control_an_altered_guard_makes_the_same_weeks_list_non_empty() -> None:
    def altered(row: str, existing: str, ledger_name: str) -> ModelJudgeVerdict:
        if (
            " | RELEASE | " in row
        ):  # the injected break: the guards refuse every RELEASE
            return ModelJudgeVerdict(
                verdict=EnumJudgeVerdict.REFUSE,
                rule="OMN-0 altered guard",
                reason="altered",
            )
        return judge_row(row, existing, ledger_name)

    entries = _week()
    assert _replay(entries).differences == ()
    broken = _replay(entries, HandlerWorkLedgerGuardReplay(judge=altered))
    assert broken.equivalent is False
    assert len(broken.differences) == 7
    assert {d.replayed.rule for d in broken.differences} == {"OMN-0 altered guard"}


def test_one_changed_verdict_does_not_cascade_into_later_rows() -> None:
    # State is the rows the live judge accepted, so a replayed refusal does not remove a row
    # from the text later rows are judged on.
    entries = _week()
    broken = _replay(
        entries,
        HandlerWorkLedgerGuardReplay(
            judge=lambda r, e, n: (
                ModelJudgeVerdict(verdict=EnumJudgeVerdict.REFUSE, rule="x", reason="x")
                if r.startswith(_stamp(WEEK_START))
                else judge_row(r, e, n)
            )
        ),
    )
    assert all(d.position <= 3 for d in broken.differences)


def test_an_empty_replay_is_equivalent_and_has_no_window() -> None:
    result = _replay([])
    assert (result.judged, result.equivalent, result.window_start) == (0, True, None)
