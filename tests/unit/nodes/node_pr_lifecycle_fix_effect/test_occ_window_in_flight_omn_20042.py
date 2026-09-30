# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A batch window is not pushed while its change-control CI is in flight (OMN-20042).

Every member opened/synchronize event rebuilt and force-pushed the repository's
window companion, and each new head cancelled the change-control run of the one
before it, so a busy window never merged and none of its members could. These
tests drive the real emitter against a bare git origin (the OMN-16336 window
harness) and prove that a member arriving while the window's run is in flight,
or while the window is armed and green, is deferred with no side effect, and
that it binds on the first rebuild after the run settles, with nothing dropped.
A conflicting window, a window held past the maximum, and a probe that cannot
read the window all still accept the rebuild.
"""

from __future__ import annotations

from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from omnimarket.events.occ_companion import EnumOccBatchMode
from omnimarket.events.pr_landing_companion import (
    EnumPrLandingCompanionDeclineCode,
    EnumPrLandingCompanionOp,
)
from omnimarket.github_api import GitHubApiError
from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_autobind_outcome_reader import (
    classify_companion_decline,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_companion_emitter import (
    OccCompanionEmitter,
)
from tests.unit.nodes.node_pr_lifecycle_fix_effect.test_occ_window_batch_companion_omn_16336 import (
    _OCC,
    _REPO,
    _TICKETS,
    _WINDOW,
    _WindowScenario,
)

_MODULE = "omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_companion_emitter"
# The harness clock starts here and advances one second per ``now()`` call.
_EPOCH = datetime(2026, 9, 27, tzinfo=UTC)


def _run(
    status: str, conclusion: str | None = None, name: str = "ci"
) -> dict[str, Any]:
    return {"name": name, "status": status, "conclusion": conclusion}


class _InFlightScenario(_WindowScenario):
    """The window harness plus the window PR's head, head commit and check-runs."""

    def __init__(self, root: Path, members: tuple[int, ...] = (101, 102)) -> None:
        super().__init__(root, members)
        self.check_runs: list[dict[str, Any]] = []
        self.auto_merge: dict[str, Any] | None = None
        # Ten minutes before the harness clock: settled, well inside the hold.
        self.head_date = _EPOCH - timedelta(minutes=10)
        self.check_run_error: GitHubApiError | None = None
        self.probed_shas: list[str] = []

    def fake_rest(
        self,
        method: str,
        path: str,
        *,
        body: object = None,
        token: str | None = None,
    ) -> dict[str, Any]:
        commits_prefix = f"/repos/{_OCC}/commits/"
        if path.startswith(commits_prefix):
            rest = path.removeprefix(commits_prefix)
            sha, _, tail = rest.partition("/")
            if not tail:
                return {
                    "sha": sha,
                    "commit": {
                        "committer": {
                            "date": self.head_date.strftime("%Y-%m-%dT%H:%M:%SZ")
                        }
                    },
                }
            assert tail.startswith("check-runs?"), path
            if self.check_run_error is not None:
                raise self.check_run_error
            self.probed_shas.append(sha)
            query = dict(
                part.split("=", maxsplit=1) for part in tail.split("?")[1].split("&")
            )
            per_page = int(query.get("per_page", "30"))
            page = int(query.get("page", "1"))
            start = (page - 1) * per_page
            return {
                "total_count": len(self.check_runs),
                "check_runs": self.check_runs[start : start + per_page],
            }
        result = super().fake_rest(method, path, body=body, token=token)
        pull_match = path.removeprefix(f"/repos/{_OCC}/pulls/")
        if pull_match.isdigit() and method == "GET":
            branch = result["head"]["ref"]
            result["head"] = {"ref": branch, "sha": self.branch_head(branch)}
            result["auto_merge"] = self.auto_merge
        return result

    def patches(self, emitter: OccCompanionEmitter) -> tuple[ExitStack, dict[str, Any]]:
        stack, mocks = super().patches(emitter)
        scenario = self

        class _ClockWithParse:
            @staticmethod
            def now(tz: object = None) -> datetime:
                scenario._clock_ticks += 1
                value = _EPOCH + timedelta(seconds=scenario._clock_ticks)
                return value if tz is not None else value.replace(tzinfo=None)

            fromisoformat = staticmethod(datetime.fromisoformat)
            strptime = staticmethod(datetime.strptime)

        stack.enter_context(patch(f"{_MODULE}.datetime", _ClockWithParse))
        return stack, mocks

    def emit_op(
        self,
        emitter: OccCompanionEmitter,
        pr_number: int,
        op: EnumPrLandingCompanionOp,
    ) -> str:
        return emitter._emit_companion_sync(
            _REPO,
            pr_number,
            _TICKETS[pr_number],
            batch_mode=EnumOccBatchMode.WINDOW,
            op=op,
        )

    def carries(self, pr_number: int) -> bool:
        return bool(self.member_receipts(_WINDOW, pr_number))


def _open_window(tmp_path: Path) -> tuple[_InFlightScenario, OccCompanionEmitter]:
    scenario = _InFlightScenario(tmp_path)
    emitter = OccCompanionEmitter()
    return scenario, emitter


def _assert_deferred(scenario: _InFlightScenario, action: str, before: str) -> None:
    assert action.startswith("skip:WINDOW_IN_FLIGHT"), action
    assert "OMN-20042" in action
    assert scenario.branch_head(_WINDOW) == before
    assert "Evidence-Source" not in scenario.bodies[102]
    assert scenario.carries(101)
    assert not scenario.carries(102)
    assert len(scenario.pull_posts) == 1


def _assert_pushed(scenario: _InFlightScenario, action: str, before: str) -> None:
    assert not action.startswith("skip:"), action
    assert scenario.branch_head(_WINDOW) != before
    assert "Evidence-Source: OCC#55" in scenario.bodies[102]
    assert scenario.carries(101)
    assert scenario.carries(102)
    assert len(scenario.pull_posts) == 1


@pytest.mark.unit
def test_window_debounce_member_during_in_flight_run_is_not_pushed(
    tmp_path: Path,
) -> None:
    scenario, emitter = _open_window(tmp_path)
    stack, mocks = scenario.patches(emitter)
    with stack:
        scenario.emit(emitter, 101)
        before = scenario.branch_head(_WINDOW)
        scenario.check_runs = [
            _run("completed", "success", "lint"),
            _run("in_progress", name="tests"),
            _run("queued", name="gate"),
        ]
        leases_before = mocks["acquire_window"].call_count
        action = scenario.emit(emitter, 102)

    _assert_deferred(scenario, action, before)
    assert "OCC#55" in action
    assert before[:8] in action
    assert "2 check run" in action
    assert scenario.probed_shas == [before]
    # No lease was taken on the deferral path.
    assert mocks["acquire_window"].call_count == leases_before


@pytest.mark.unit
def test_window_debounce_member_lands_in_next_rebuild(tmp_path: Path) -> None:
    scenario, emitter = _open_window(tmp_path)
    stack, _mocks = scenario.patches(emitter)
    with stack:
        scenario.emit(emitter, 101)
        before = scenario.branch_head(_WINDOW)
        scenario.check_runs = [_run("in_progress", name="tests")]
        _assert_deferred(scenario, scenario.emit(emitter, 102), before)

        scenario.check_runs = [_run("completed", "success", name="tests")]
        action = scenario.emit(emitter, 102)

    _assert_pushed(scenario, action, before)


@pytest.mark.unit
def test_window_debounce_window_without_in_flight_run_accepts_member(
    tmp_path: Path,
) -> None:
    scenario, emitter = _open_window(tmp_path)
    stack, _mocks = scenario.patches(emitter)
    with stack:
        scenario.emit(emitter, 101)
        before = scenario.branch_head(_WINDOW)
        scenario.check_runs = [
            _run("completed", "success", "lint"),
            _run("completed", "skipped", "tests"),
        ]
        action = scenario.emit(emitter, 102)

    _assert_pushed(scenario, action, before)


@pytest.mark.unit
def test_window_debounce_in_flight_run_on_a_later_page_defers(
    tmp_path: Path,
) -> None:
    scenario, emitter = _open_window(tmp_path)
    stack, _mocks = scenario.patches(emitter)
    with stack:
        scenario.emit(emitter, 101)
        before = scenario.branch_head(_WINDOW)
        scenario.check_runs = [
            _run("completed", "success", f"job-{n}") for n in range(150)
        ] + [_run("in_progress", name="late")]
        action = scenario.emit(emitter, 102)

    _assert_deferred(scenario, action, before)
    assert "1 check run" in action


@pytest.mark.unit
def test_window_debounce_armed_green_window_defers(tmp_path: Path) -> None:
    scenario, emitter = _open_window(tmp_path)
    stack, _mocks = scenario.patches(emitter)
    with stack:
        scenario.emit(emitter, 101)
        before = scenario.branch_head(_WINDOW)
        scenario.check_runs = [_run("completed", "success", "tests")]
        scenario.auto_merge = {"merge_method": "squash"}
        action = scenario.emit(emitter, 102)

    _assert_deferred(scenario, action, before)


@pytest.mark.unit
@pytest.mark.parametrize(
    "conclusion",
    [
        "failure",
        "timed_out",
        "cancelled",
        "action_required",
        "startup_failure",
        "stale",
    ],
)
def test_window_debounce_armed_red_window_accepts_rebuild(
    tmp_path: Path, conclusion: str
) -> None:
    scenario, emitter = _open_window(tmp_path)
    stack, _mocks = scenario.patches(emitter)
    with stack:
        scenario.emit(emitter, 101)
        before = scenario.branch_head(_WINDOW)
        scenario.check_runs = [
            _run("completed", "success", "lint"),
            _run("completed", conclusion, "tests"),
        ]
        scenario.auto_merge = {"merge_method": "squash"}
        action = scenario.emit(emitter, 102)

    _assert_pushed(scenario, action, before)


@pytest.mark.unit
def test_window_debounce_conflicting_window_accepts_rebuild(tmp_path: Path) -> None:
    scenario, emitter = _open_window(tmp_path)
    stack, _mocks = scenario.patches(emitter)
    with stack:
        scenario.emit(emitter, 101)
        before = scenario.branch_head(_WINDOW)
        scenario.check_runs = [_run("in_progress", name="tests")]
        scenario.occ_prs[55]["mergeable"] = False
        scenario.occ_prs[55]["mergeable_state"] = "dirty"
        action = scenario.emit(emitter, 102)

    _assert_pushed(scenario, action, before)


@pytest.mark.unit
def test_window_debounce_max_hold_releases(tmp_path: Path) -> None:
    scenario, emitter = _open_window(tmp_path)
    stack, _mocks = scenario.patches(emitter)
    with stack:
        scenario.emit(emitter, 101)
        before = scenario.branch_head(_WINDOW)
        scenario.check_runs = [_run("in_progress", name="stuck")]
        scenario.head_date = _EPOCH - timedelta(hours=1)
        action = scenario.emit(emitter, 102)

    _assert_pushed(scenario, action, before)


@pytest.mark.unit
def test_window_debounce_fresh_push_without_runs_defers(tmp_path: Path) -> None:
    scenario, emitter = _open_window(tmp_path)
    stack, _mocks = scenario.patches(emitter)
    with stack:
        scenario.emit(emitter, 101)
        before = scenario.branch_head(_WINDOW)
        scenario.check_runs = []
        scenario.head_date = _EPOCH
        action = scenario.emit(emitter, 102)

    _assert_deferred(scenario, action, before)


@pytest.mark.unit
def test_window_debounce_settled_push_without_runs_accepts_member(
    tmp_path: Path,
) -> None:
    scenario, emitter = _open_window(tmp_path)
    stack, _mocks = scenario.patches(emitter)
    with stack:
        scenario.emit(emitter, 101)
        before = scenario.branch_head(_WINDOW)
        scenario.check_runs = []
        action = scenario.emit(emitter, 102)

    _assert_pushed(scenario, action, before)


@pytest.mark.unit
def test_window_debounce_regenerate_is_exempt(tmp_path: Path) -> None:
    scenario, emitter = _open_window(tmp_path)
    stack, _mocks = scenario.patches(emitter)
    with stack:
        scenario.emit(emitter, 101)
        before = scenario.branch_head(_WINDOW)
        scenario.check_runs = [_run("in_progress", name="tests")]
        action = scenario.emit_op(emitter, 102, EnumPrLandingCompanionOp.REGENERATE)

    _assert_pushed(scenario, action, before)
    assert scenario.probed_shas == []


@pytest.mark.unit
def test_window_debounce_probe_error_fails_open(tmp_path: Path) -> None:
    scenario, emitter = _open_window(tmp_path)
    stack, _mocks = scenario.patches(emitter)
    with stack:
        scenario.emit(emitter, 101)
        before = scenario.branch_head(_WINDOW)
        scenario.check_runs = [_run("in_progress", name="tests")]
        scenario.check_run_error = GitHubApiError("boom", status_code=502)
        action = scenario.emit(emitter, 102)

    _assert_pushed(scenario, action, before)


@pytest.mark.unit
def test_window_debounce_first_member_opens_a_new_window(tmp_path: Path) -> None:
    """No open window: the first member mints it, the probe never blocks it."""
    scenario, emitter = _open_window(tmp_path)
    stack, _mocks = scenario.patches(emitter)
    with stack:
        scenario.check_runs = [_run("in_progress", name="tests")]
        action = scenario.emit(emitter, 101)

    assert not action.startswith("skip:"), action
    assert "Evidence-Source: OCC#55" in scenario.bodies[101]
    assert scenario.probed_shas == []


@pytest.mark.unit
def test_window_debounce_decline_code_classified() -> None:
    code, occ_pr, stamped = classify_companion_decline("skip:WINDOW_IN_FLIGHT — x")
    assert code is EnumPrLandingCompanionDeclineCode.WINDOW_IN_FLIGHT
    assert occ_pr is None
    assert stamped is None
