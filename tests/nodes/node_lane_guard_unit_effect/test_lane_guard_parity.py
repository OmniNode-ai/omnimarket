# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-22991 parity: the omnimarket lane guard never signals a process it only saw once under a recycled pid, and a lane that
must be stopped is told first.

Tonight (2026-11-07) four lab lanes ended "no result event (engine rc=143)". On h101 (a Mac, where the guard
runs in ``python-watchdog`` mode and follows the command's process tree because ``ps`` hides the environment)
three of them died in the same second a *neighbouring* lane's guard ended: the neighbour's guard reaped every
process whose pid it had ever seen in its own tree, and the identity test (``age + 2 >= age at first sight``)
holds for any process that is older than two seconds. A pid is recycled every ~60 minutes on a Mac (pid_max
99999), so the neighbour's reap sent SIGTERM (then SIGKILL) to another lane's guard, run script or engine.

These tests replace the system: the process table and the clock are injected, so each control is exact.
"""

from __future__ import annotations

import json
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from omnimarket.nodes.node_lane_guard_unit_effect import (
    runtime_lane_guard_host as gh,
)

HOST = Path(gh.__file__)


def _end_line(stderr: str) -> dict:
    """The JSON of the LANE-GUARD-END line the guard printed."""
    for raw in stderr.splitlines():
        if raw.startswith("LANE-GUARD-END "):
            return json.loads(raw[len("LANE-GUARD-END ") :])
    raise AssertionError("no LANE-GUARD-END line in: " + stderr[-400:])


def _no_systemd(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args, 1, "", "")


def _track(monkeypatch, first_sight: list, at: float) -> dict:
    """What the tree tracker holds after one pass at clock ``at`` over ``first_sight`` (processes alive then)."""
    monkeypatch.setattr(
        gh,
        "time",
        SimpleNamespace(time=lambda: at, monotonic=time.monotonic, sleep=time.sleep),
    )
    tracker = gh._TreeTracker(100)

    def one_pass() -> list:
        tracker.stop.set()
        return first_sight

    monkeypatch.setattr(gh, "read_proc_table", one_pass)
    tracker.run()
    return tracker.seen


def _reap(monkeypatch, table: list, tracked: dict, at: float) -> list[int]:
    monkeypatch.setattr(
        gh,
        "time",
        SimpleNamespace(time=lambda: at, monotonic=time.monotonic, sleep=time.sleep),
    )
    killed: list[int] = []

    def fake_kill(pids: list[int]) -> dict[int, str]:
        killed.extend(pids)
        return dict.fromkeys(pids, "term")

    gh.reap_unit(
        "onexlane-lane-a-1",
        run=_no_systemd,
        read_table=lambda: table,
        kill=fake_kill,
        tracked=tracked,
    )
    return killed


def test_parity_a_recycled_pid_is_not_a_descendant_of_the_lane(monkeypatch) -> None:
    """The failing control. A child of lane A (pid 500) was seen 0.2 s after it started and then exited. 600 s
    later pid 500 is another lane's engine, started after that child died. Reaping lane A must not touch it."""
    seen = _track(monkeypatch, [gh.Proc(500, 100, 0.2, 5, "git status")], at=10_000.0)
    other_lanes_engine = gh.Proc(
        500, 1, 250.0, 5, "claude -p brief --model sonnet", tag=None
    )
    assert _reap(monkeypatch, [other_lanes_engine], seen, at=10_600.0) == []


def test_parity_a_recycled_pid_is_not_signalled_even_when_it_is_older_than_two_seconds(
    monkeypatch,
) -> None:
    """The old test was ``age + 2 >= age at first sight``: any process older than two seconds passed it."""
    seen = _track(monkeypatch, [gh.Proc(501, 100, 0.0, 5, "sleep 1")], at=50_000.0)
    stranger = gh.Proc(501, 1, 3.5, 5, "bash run.sh", tag=None)
    assert _reap(monkeypatch, [stranger], seen, at=50_010.0) == []


def test_parity_a_child_that_left_the_tree_is_still_the_same_process_and_is_reaped(
    monkeypatch,
) -> None:
    """The positive control: the tracker exists so that a child reparented to init (a ``setsid`` daemon) is
    still found. Same pid, same start time: it is the lane's, and it goes."""
    seen = _track(
        monkeypatch, [gh.Proc(502, 100, 0.3, 5, "python helper.py")], at=20_000.0
    )
    reparented = gh.Proc(502, 1, 600.3, 5, "python helper.py", tag=None)
    assert _reap(monkeypatch, [reparented], seen, at=20_600.0) == [502]


def test_parity_the_start_time_check_tolerates_ps_second_resolution(
    monkeypatch,
) -> None:
    """``ps`` reports elapsed time in whole seconds, so the same process reads up to two seconds apart."""
    seen = _track(monkeypatch, [gh.Proc(503, 100, 4.0, 5, "node")], at=30_000.0)
    same = gh.Proc(503, 1, 604.0 + 1.0, 5, "node", tag=None)
    assert _reap(monkeypatch, [same], seen, at=30_600.0) == [503]


def test_parity_a_tagged_process_is_still_reaped_whatever_its_pid_history(
    monkeypatch,
) -> None:
    tagged = gh.Proc(504, 1, 9.0, 5, "pytest", tag="onexlane-lane-a-1", deadline=1.0)
    assert _reap(monkeypatch, [tagged], {}, at=1.0) == [504]


# The three kills named from the h101 LANE-GUARD-END residue lists on 2026-11-07 (pid, age in seconds at the kill, the
# victim lane's process, the neighbour whose end-of-lane reap sent the signal, the kill time on the clock below).
RECORDED_KILLS = [
    (
        63361,
        317.0,
        "piece2-canary-1035 killed pieces-prs-1100's guard",
        "python3 -c lane_guard run --lane pieces-prs-1100",
    ),
    (
        30629,
        93.0,
        "job-exit-1120 killed floorfill-t15d-1140's run script",
        "bash run.sh floorfill-t15d-1140",
    ),
    (
        88978,
        621.0,
        "floor-prs-b-1140 killed t8-fold-1220's engine",
        "claude -p brief --model sonnet",
    ),
]


@pytest.mark.parametrize(
    ("pid", "victim_age", "label", "victim_command"), RECORDED_KILLS
)
def test_parity_each_recorded_peer_kill_on_h101_no_longer_reaches_the_victim(
    monkeypatch, pid, victim_age, label, victim_command
) -> None:
    """Replay of a recorded pid. The neighbour's tracker saw the pid on a short-lived child of its own tree (the
    old test passed it for any process older than two seconds); at the end of the neighbour's lane the pid
    belongs to the victim, whose age is the recorded one. The failing control is the same replay on the
    previous guard source, which returns the pid."""
    seen = _track(
        monkeypatch, [gh.Proc(pid, 100, 0.4, 5, "git rev-parse HEAD")], at=40_000.0
    )
    victim = gh.Proc(pid, 1, victim_age, 5, victim_command, tag=None)
    assert _reap(monkeypatch, [victim], seen, at=40_000.0 + victim_age + 5.0) == [], (
        label
    )


# ---------------------------------------------------------------------------------------------
# A lane that must be stopped is told first (the guard's own stop; real processes).
# ---------------------------------------------------------------------------------------------

REPORTING = (
    "import signal,sys,time\n"
    "def report(*_):\n"
    "    open(sys.argv[1],'w').write('REPORT-WRITTEN')\n"
    "    sys.exit(0)\n"
    "signal.signal(signal.SIGINT, report)\n"
    "open(sys.argv[2],'w').write('up')\n"
    "time.sleep(120)\n"
)

IGNORING = (
    "import signal,sys,time\n"
    "signal.signal(signal.SIGINT, signal.SIG_IGN)\n"
    "open(sys.argv[1],'w').write('up')\n"
    "time.sleep(120)\n"
)


def _guarded(
    tmp_path: Path, body: str, *extra: str, files: tuple[str, ...]
) -> subprocess.Popen[str]:
    paths = [str(tmp_path / name) for name in files]
    return subprocess.Popen(
        [
            sys.executable,
            str(HOST),
            "run",
            "--unit",
            f"onexlane-test-{tmp_path.name[-8:]}",
            "--runtime-s",
            "100",
            *extra,
            "--",
            sys.executable,
            "-c",
            body,
            *paths,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _wait_for(path: Path, seconds: float = 20.0) -> None:
    end = time.monotonic() + seconds
    while not path.exists() and time.monotonic() < end:
        time.sleep(0.05)
    assert path.exists(), f"{path.name} never appeared"


def test_parity_a_stop_gives_the_lane_a_sigint_first_so_it_can_write_its_report(
    tmp_path: Path,
) -> None:
    """The failing control for the kill itself: a SIGTERM to the guard used to SIGTERM the engine at once, and a
    ``claude -p`` run ends 143 with no result event (measured on h053: SIGTERM mid-tool, rc 143, no result
    event; SIGINT, rc 0 and a ``result`` event). The guard now sends SIGINT first and waits for the report."""
    proc = _guarded(tmp_path, REPORTING, files=("report.txt", "up.txt"))
    _wait_for(tmp_path / "up.txt")
    t0 = time.monotonic()
    proc.send_signal(signal.SIGTERM)
    _out, err = proc.communicate(timeout=60)
    end = _end_line(err)
    assert (tmp_path / "report.txt").read_text() == "REPORT-WRITTEN"
    assert time.monotonic() - t0 < 15, (
        "a command that reports at once is not held for the whole notice"
    )
    assert end["stopped_by_signal"] == signal.SIGTERM
    assert end["stop_phase"] == "notice"
    assert end["rc"] == 0


def test_parity_a_command_that_ignores_the_notice_is_terminated_when_the_notice_ends(
    tmp_path: Path,
) -> None:
    proc = _guarded(tmp_path, IGNORING, "--stop-notice-s", "1", files=("up.txt",))
    _wait_for(tmp_path / "up.txt")
    t0 = time.monotonic()
    proc.send_signal(signal.SIGTERM)
    _out, err = proc.communicate(timeout=60)
    end = _end_line(err)
    assert time.monotonic() - t0 < 30
    assert end["stop_phase"] == "term"
    assert end["stopped_by_signal"] == signal.SIGTERM
    assert end["rc"] != 0


def test_parity_a_tests_run_is_not_given_a_notice(tmp_path: Path) -> None:
    """Only an engine lane writes a report; a pytest run under ``--kind tests`` stops as before."""
    proc = _guarded(
        tmp_path,
        IGNORING,
        "--kind",
        "tests",
        "--stop-notice-s",
        "30",
        files=("up.txt",),
    )
    _wait_for(tmp_path / "up.txt")
    t0 = time.monotonic()
    proc.send_signal(signal.SIGTERM)
    _out, err = proc.communicate(timeout=60)
    assert time.monotonic() - t0 < 25, (
        "a tests run waited out a notice it was never given"
    )
    assert _end_line(err)["stop_phase"] == "term"


# ---------------------------------------------------------------------------------------------
# The deadline is a stop too: the OS limit (RuntimeMaxSec, timeout) is only the backstop behind it.
# ---------------------------------------------------------------------------------------------


def test_parity_a_lane_that_reaches_its_deadline_is_told_first_in_every_mode(
    tmp_path: Path,
) -> None:
    """The failing control for the scope-mode kill: ``RuntimeMaxSec`` (and ``timeout``) sent SIGTERM at the deadline
    and the engine ended 143 with no result event, because the guard's own notice ran only in python-watchdog mode.
    The guard now enforces an engine lane's deadline itself, in whatever mode the host selects."""
    proc = _guarded(
        tmp_path, REPORTING, "--runtime-s", "3", files=("report.txt", "up.txt")
    )
    _out, err = proc.communicate(timeout=90)
    end = _end_line(err)
    assert (tmp_path / "report.txt").read_text() == "REPORT-WRITTEN"
    assert end["stop_phase"] == "notice"
    assert end["deadline_hit"] is True
    assert end["stopped_by_signal"] is None
    assert end["rc"] == 0


def test_parity_a_tests_run_keeps_the_os_limit_as_its_deadline(tmp_path: Path) -> None:
    proc = _guarded(
        tmp_path, IGNORING, "--kind", "tests", "--runtime-s", "3", files=("up.txt",)
    )
    _out, err = proc.communicate(timeout=90)
    end = _end_line(err)
    assert end["deadline_hit"] is True
    assert end["stop_phase"] is None or end["stop_phase"] == "term"
    assert end["rc"] != 0


def test_parity_the_os_limit_of_an_engine_lane_sits_behind_the_guards_notice() -> None:
    lane = gh.Limits(600 + gh.GRACE_S, 8 * 1024**3, 400, kind="lane")
    tests = gh.Limits(600 + gh.GRACE_S, 8 * 1024**3, 400, kind="tests")
    behind = gh.STOP_NOTICE_S + gh.STOP_TIMEOUT_S
    scope = gh.scope_argv("onexlane-x-1", lane, ["claude"])
    assert f"RuntimeMaxSec={lane.runtime_s + behind}" in scope
    assert gh.timeout_argv(lane, ["claude"])[2] == str(lane.runtime_s + behind)
    assert f"RuntimeMaxSec={tests.runtime_s}" in gh.scope_argv(
        "onexlane-x-1", tests, ["pytest"]
    )
    assert gh.timeout_argv(tests, ["pytest"])[2] == str(tests.runtime_s)
    assert gh.backstop_runtime_s(lane, 0) == lane.runtime_s
