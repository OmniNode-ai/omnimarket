# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""CI Summary waits a bounded window for a red external context (OMN-18808).

WHAT IS UNDER TEST
    The REAL L4 external-context resolution in ``scripts/ci/ci_summary_gate.py``,
    imported as the module CI runs.

THE INCIDENT
    ``CI Summary`` records a terminal FAILURE on an external context that is
    red only because the change-control evidence companion has not been minted
    yet, and then never re-polls. On a ticketed PR the companion is minted by
    AUTOMATION after the PR opens; until it lands the PR body carries no
    evidence-source stamp and the Receipt Gate is legitimately red. When the
    companion merges, automation PATCHes the PR body, the body edit re-fires
    every workflow whose ``types:`` include ``edited``, and the Receipt Gate
    re-runs and goes green ON ITS OWN. By then ``CI Summary`` has already
    exited, and the armed auto-merge is held by a verdict no longer true of the
    head. Only a human rerun cleared it, and that rerun passed with NO CHANGE
    TO THE PR.

    Measured in ``omnibase_infra`` over 30 merged ``dev`` PRs: 16 exhibited the
    shape, every one recovered, the slowest in 6.8 minutes, the median in 1.9.
    Landed there as ``#3793`` (squash ``69ba4fc5``).

WHAT OMN-15727 ASKED FOR, AND WHAT WAS ALREADY TRUE
    OMN-15727's symptom 1 is two things, and only one of them was still broken
    here. Reading a STALE attempt when a newer one exists was ALREADY FIXED in
    this copy: :class:`TestTheOmn15727Timeline` replays the omnimarket#2024
    shape -- failure, failure, failure, success, success on one unchanged head
    -- and it resolves SUCCESS on the pre-OMN-18808 module. That was verified
    against the unmodified module before this change was written, and the class
    is a REGRESSION PIN, not a claim of credit. EXITING on a red an automatic
    re-run is about to replace is the half that was still open, and that is
    what the windows below close.

THE OMN-15112 PROTECTION IS PRESERVED, AND IT IS WHY THIS COPY DIFFERS
    This repository asserts two contexts that ~52 independent caller workflows
    each mint against one SHA. Their rows tie on the second-granular
    ``started_at`` routinely, and such a tie is NOT a rerun history. So
    resolution here is ``(started_at, severity, id)``, not the
    ``(started_at, id)`` its sibling copies use: the more-blocking row still
    wins a tie, so a red sibling can never hide behind a green one.
    :class:`TestNewestAttemptSelection` pins the whole ordering, with every
    window deliberately EXPIRED so it proves resolution on its own.

THIS DOES NOT REOPEN THE SKIP-AS-PASS VECTOR (OMN-15057 / OMN-14854)
    That vector is ``skipped`` read as SUCCESS.
    :class:`TestSkippedIsNeverSuccess` is the control.
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime, timedelta

import pytest

from scripts.ci import ci_summary_gate as gate
from scripts.ci.ci_summary_gate import (
    CANCELLED_SUPERSESSION_GRACE_S,
    EXIT_FAILURE,
    EXIT_PENDING,
    EXIT_SUCCESS,
    EXTERNAL_FAILURE_SUPERSESSION_GRACE_S,
    SUPERSEDABLE_CONCLUSIONS,
    CheckRunState,
    cancellation_is_provisional,
    dedup_latest_check_runs,
    evaluate_external,
    supersedable_verdict_is_provisional,
    verdict_is_provisional,
)

pytestmark = pytest.mark.unit

CONTEXT = "verify / verify"
DUPLICATED = "occ-preflight / eligibility"
HEAD = "3" * 40
NOW = datetime(2026, 9, 19, 3, 5, 36, tzinfo=UTC)

#: Far enough past every window that no case in a class using it can pass
#: because of a grace rather than because of the property under test.
EXPIRED_S = EXTERNAL_FAILURE_SUPERSESSION_GRACE_S + 3600


def _z(when: datetime) -> str:
    return when.isoformat().replace("+00:00", "Z")


def _row(
    conclusion: str | None,
    *,
    age_s: float,
    run_id: int = 1,
    name: str = CONTEXT,
    status: str = "completed",
    started_age_s: float | None = None,
    completed_at: str | None | object = ...,
) -> dict[str, object]:
    """One check-run row that concluded ``age_s`` seconds before :data:`NOW`."""

    completed = NOW - timedelta(seconds=age_s)
    started = NOW - timedelta(
        seconds=age_s + 30 if started_age_s is None else started_age_s
    )
    row: dict[str, object] = {
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "id": run_id,
        "head_sha": HEAD,
        "started_at": _z(started),
        "completed_at": _z(completed) if completed_at is ... else completed_at,
    }
    if status != "completed":
        row["conclusion"] = None
        row["completed_at"] = None
    return row


def _state(
    conclusion: str | None, *, age_s: float, completed_at: str | None | object = ...
) -> CheckRunState:
    completed = NOW - timedelta(seconds=age_s)
    return CheckRunState(
        name=CONTEXT,
        status="completed",
        conclusion=conclusion,
        started_at=_z(completed - timedelta(seconds=30)),
        id=1,
        completed_at=_z(completed) if completed_at is ... else completed_at,
    )


def _external(
    rows: list[dict[str, object]] | None,
    *,
    now: datetime | None = NOW,
    expected: tuple[str, ...] = (CONTEXT,),
) -> tuple[int, str]:
    return evaluate_external(rows, expected=expected, now=now)


class TestAFreshRedIsPendingNotFailed:
    """AC1 -- the behaviour the ticket exists for."""

    @pytest.mark.parametrize("conclusion", ["failure", "skipped", "cancelled"])
    def test_inside_its_window_the_context_is_pending(self, conclusion: str) -> None:
        code, report = _external([_row(conclusion, age_s=38)])
        assert code == EXIT_PENDING, report
        assert "external-context failures" not in report

    @pytest.mark.parametrize("conclusion", ["failure", "skipped", "cancelled"])
    def test_outside_its_window_it_still_fails(self, conclusion: str) -> None:
        code, report = _external([_row(conclusion, age_s=EXPIRED_S)])
        assert code == EXIT_FAILURE
        assert CONTEXT in report

    def test_the_two_windows_are_separate_and_the_cancelled_one_is_shorter(
        self,
    ) -> None:
        assert CANCELLED_SUPERSESSION_GRACE_S < EXTERNAL_FAILURE_SUPERSESSION_GRACE_S
        between = CANCELLED_SUPERSESSION_GRACE_S + 60
        assert not cancellation_is_provisional(_state("cancelled", age_s=between), NOW)
        assert supersedable_verdict_is_provisional(
            _state("failure", age_s=between), NOW
        )

    def test_the_supersedable_set_is_failure_and_skipped_only(self) -> None:
        assert frozenset({"failure", "skipped"}) == SUPERSEDABLE_CONCLUSIONS

    def test_the_pending_reason_is_reported_distinctly(self) -> None:
        _code, report = _external([_row("failure", age_s=38)])
        assert "awaiting an automatic replacement" in report

    def test_a_settled_red_is_not_reported_as_awaiting_anything(self) -> None:
        _code, report = _external([_row("failure", age_s=EXPIRED_S)])
        assert "awaiting an automatic replacement" not in report


class TestTheOmn15727Timeline:
    """OMN-15727 -- a regression PIN, not a claim of credit.

    The omnimarket#2024 head carried five same-name rows: three failures then
    two successes, with no push between them. Reading the newest of those was
    ALREADY correct in this copy before OMN-18808; this class stops a future
    change from quietly reintroducing the stale read, which is the half of that
    ticket's symptom 1 that was never broken here.
    """

    def _timeline(self) -> list[dict[str, object]]:
        return [
            _row("failure", age_s=5000, run_id=1, name=DUPLICATED),
            _row("failure", age_s=4990, run_id=2, name=DUPLICATED),
            _row("failure", age_s=4980, run_id=3, name=DUPLICATED),
            _row("success", age_s=4000, run_id=4, name=DUPLICATED),
            _row("success", age_s=3900, run_id=5, name=DUPLICATED),
        ]

    def test_it_resolves_success_not_the_stale_fail(self) -> None:
        code, report = _external(self._timeline(), expected=(DUPLICATED,))
        assert code == EXIT_SUCCESS, report

    def test_it_resolves_success_with_no_clock_at_all(self) -> None:
        """Nothing about this depends on the new windows."""

        code, _report = _external(self._timeline(), expected=(DUPLICATED,), now=None)
        assert code == EXIT_SUCCESS

    def test_payload_order_does_not_change_the_verdict(self) -> None:
        code, _report = _external(
            list(reversed(self._timeline())), expected=(DUPLICATED,)
        )
        assert code == EXIT_SUCCESS


class TestUngracedConclusionsStayTerminal:
    """``timed_out`` and ``action_required`` are not a re-run shape."""

    @pytest.mark.parametrize("conclusion", ["timed_out", "action_required", "neutral"])
    @pytest.mark.parametrize("age_s", [0, 38, 601, 1201])
    def test_they_fail_on_the_poll_that_observes_them(
        self, conclusion: str, age_s: float
    ) -> None:
        assert not verdict_is_provisional(_state(conclusion, age_s=age_s), NOW)
        code, _report = _external([_row(conclusion, age_s=age_s)])
        assert code == EXIT_FAILURE


class TestFailClosedOnUncertainty:
    """AC4 -- every uncertain input restores the strict pre-grace reading."""

    @pytest.mark.parametrize("conclusion", ["failure", "skipped", "cancelled"])
    def test_no_clock_fails_now(self, conclusion: str) -> None:
        assert not verdict_is_provisional(_state(conclusion, age_s=38), None)
        code, _report = _external([_row(conclusion, age_s=38)], now=None)
        assert code == EXIT_FAILURE

    @pytest.mark.parametrize("completed_at", [None, "", "not-a-timestamp"])
    def test_an_unreadable_completion_time_fails_now(
        self, completed_at: str | None
    ) -> None:
        assert not verdict_is_provisional(
            _state("failure", age_s=38, completed_at=completed_at), NOW
        )
        code, _report = _external(
            [_row("failure", age_s=38, completed_at=completed_at)]
        )
        assert code == EXIT_FAILURE

    def test_a_completion_far_in_the_future_fails_now(self) -> None:
        skewed = -(EXTERNAL_FAILURE_SUPERSESSION_GRACE_S + 60)
        assert not verdict_is_provisional(_state("failure", age_s=skewed), NOW)
        code, _report = _external([_row("failure", age_s=skewed)])
        assert code == EXIT_FAILURE

    def test_ordinary_clock_skew_stays_provisional(self) -> None:
        """A ``completed_at`` seconds in the future is skew, not a wrong clock."""
        assert verdict_is_provisional(_state("failure", age_s=-5), NOW)

    def test_an_absent_context_is_pending_never_green(self) -> None:
        code, _report = _external([])
        assert code == EXIT_PENDING

    def test_an_unfetchable_payload_is_pending_never_green(self) -> None:
        code, _report = _external(None)
        assert code == EXIT_PENDING

    def test_a_still_running_context_is_pending_never_green(self) -> None:
        code, _report = _external([_row(None, age_s=0, status="in_progress")])
        assert code == EXIT_PENDING


class TestSkippedIsNeverSuccess:
    """AC4's other half -- the skip-as-pass control (OMN-15057 / OMN-14854)."""

    @pytest.mark.parametrize(
        ("age_s", "now"),
        [(38, None), (38, NOW), (EXPIRED_S, NOW)],
        ids=["no-clock", "inside-grace", "past-grace"],
    )
    def test_a_lone_skip_never_resolves_the_context(
        self, age_s: float, now: datetime | None
    ) -> None:
        code, _report = _external([_row("skipped", age_s=age_s)], now=now)
        assert code != EXIT_SUCCESS

    def test_a_lone_skip_fails_once_the_window_expires(self) -> None:
        code, _report = _external([_row("skipped", age_s=EXPIRED_S)])
        assert code == EXIT_FAILURE

    def test_a_real_success_supersedes_a_fresh_skip(self) -> None:
        code, _report = _external(
            [_row("skipped", age_s=38, run_id=1), _row("success", age_s=0, run_id=2)]
        )
        assert code == EXIT_SUCCESS


class TestNewestAttemptSelection:
    """AC2 -- ``(started_at, severity, id)``, with every window EXPIRED.

    Nothing in this class can pass because a window held a row provisional:
    every row is older than :data:`EXPIRED_S`.
    """

    def test_a_newer_success_beats_a_stale_failure(self) -> None:
        code, _report = _external(
            [
                _row("failure", age_s=EXPIRED_S + 600, run_id=1),
                _row("success", age_s=EXPIRED_S, run_id=2),
            ]
        )
        assert code == EXIT_SUCCESS

    def test_payload_row_order_does_not_decide(self) -> None:
        code, _report = _external(
            [
                _row("success", age_s=EXPIRED_S, run_id=2),
                _row("failure", age_s=EXPIRED_S + 600, run_id=1),
            ]
        )
        assert code == EXIT_SUCCESS

    def test_a_newer_failure_after_a_success_still_fails(self) -> None:
        code, _report = _external(
            [
                _row("success", age_s=EXPIRED_S + 600, run_id=1),
                _row("failure", age_s=EXPIRED_S, run_id=2),
            ]
        )
        assert code == EXIT_FAILURE

    def test_three_attempts_resolve_on_the_newest_not_the_worst(self) -> None:
        code, _report = _external(
            [
                _row("success", age_s=EXPIRED_S + 1200, run_id=1),
                _row("failure", age_s=EXPIRED_S + 600, run_id=2),
                _row("success", age_s=EXPIRED_S, run_id=3),
            ]
        )
        assert code == EXIT_SUCCESS

    def test_a_newer_running_attempt_is_pending_not_a_stale_failure(self) -> None:
        code, _report = _external(
            [
                _row("failure", age_s=EXPIRED_S + 600, run_id=1),
                _row(None, age_s=EXPIRED_S, run_id=2, status="in_progress"),
            ]
        )
        assert code == EXIT_PENDING

    def test_severity_still_outranks_the_id_on_a_tied_started_at(self) -> None:
        """The OMN-15112 protection, and why this copy is not its siblings.

        Two concurrent producers of one context name post in the same second.
        The green one carries the HIGHER check-run id. Resolving that tie by id
        -- which is what the sibling copies in omnibase_core and omnibase_infra
        do -- would drop the red. Here the more-blocking row wins.
        """

        tied = EXPIRED_S
        code, _report = _external(
            [
                _row(
                    "failure",
                    age_s=tied,
                    run_id=1,
                    name=DUPLICATED,
                    started_age_s=tied,
                ),
                _row(
                    "success",
                    age_s=tied,
                    run_id=99,
                    name=DUPLICATED,
                    started_age_s=tied,
                ),
            ],
            expected=(DUPLICATED,),
        )
        assert code == EXIT_FAILURE

    def test_a_tie_on_started_at_and_severity_is_broken_by_the_id_not_by_order(
        self,
    ) -> None:
        """The OMN-16332 defect in this copy, stated as a test.

        Two rows tied on ``started_at`` AND equally blocking. Before this
        change whichever came LAST in the payload array survived, and the
        check-runs endpoint guarantees no ordering. The id decides it now, and
        the same set of rows resolves the same way in either order.
        """

        tied = EXPIRED_S
        rows = [
            _row("failure", age_s=tied, run_id=1, started_age_s=tied),
            _row("failure", age_s=tied, run_id=2, started_age_s=tied),
        ]
        forward = dedup_latest_check_runs(rows)
        reverse = dedup_latest_check_runs(list(reversed(rows)))
        assert forward[CONTEXT].id == reverse[CONTEXT].id == 2

    def test_a_row_with_no_id_loses_a_tie_rather_than_winning_by_position(
        self,
    ) -> None:
        tied = EXPIRED_S
        without = _row("failure", age_s=tied, started_age_s=tied)
        del without["id"]
        with_id = _row("failure", age_s=tied, run_id=7, started_age_s=tied)
        assert dedup_latest_check_runs([without, with_id])[CONTEXT].id == 7
        assert dedup_latest_check_runs([with_id, without])[CONTEXT].id == 7


class TestTheCliHandsTheGateAClock:
    """AC5 -- the un-forgeability of both windows rests on this.

    Both windows return ``False`` when ``now`` is ``None`` -- deliberate and
    fail-closed, so a caller that forgets the time enforces the old strict
    reading rather than waiting on a red forever. That is the right default and
    a terrible SILENT outcome: the first port of this change into a sibling
    repository changed the gate module and not its poller, and the gate shipped
    completely inert with every unit test green and mypy clean.

    No flag is the load-bearing half. A caller-assertable observation time
    would let a long-dead red be held provisional indefinitely, which is the
    one way these windows could become a bypass. It is asserted BEHAVIOURALLY
    -- by invoking the CLI with each candidate flag and requiring a parse error
    -- so an option added by any route is caught, not only one spelled the way
    this file guesses.
    """

    FLAGS = ("--now", "--observed-at", "--clock", "--as-of", "--at")

    @pytest.mark.parametrize("flag", FLAGS)
    def test_no_cli_option_supplies_the_observation_time(
        self, flag: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises((SystemExit, argparse.ArgumentError)):
            gate.main(["--jobs-file", "-", flag, _z(NOW)])
        captured = capsys.readouterr()
        assert "unrecognized arguments" in captured.err or "invalid" in captured.err

    def test_main_reaches_the_external_layer_with_a_current_aware_clock(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        jobs = tmp_path / "jobs.json"
        jobs.write_text(json.dumps([]), encoding="utf-8")
        runs = tmp_path / "check_runs.json"
        runs.write_text(json.dumps([]), encoding="utf-8")

        seen: list[datetime | None] = []
        real = gate.evaluate_external

        def _spy(*args: object, **kwargs: object):
            seen.append(kwargs.get("now"))
            return real(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(gate, "evaluate_external", _spy)
        before = datetime.now(UTC)
        gate.main(
            [
                "--jobs-file",
                str(jobs),
                "--check-runs-file",
                str(runs),
                "--report-only",
            ]
        )
        after = datetime.now(UTC)

        assert len(seen) == 1
        now = seen[0]
        assert isinstance(now, datetime)
        assert now.tzinfo is not None, "a naive clock cannot be compared to GitHub's"
        assert before <= now <= after, "the clock must be read at call time"
