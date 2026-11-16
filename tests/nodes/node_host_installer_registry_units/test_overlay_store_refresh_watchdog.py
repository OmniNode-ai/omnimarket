# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-24839: the overlay store refresh tick ends at its deadline, and its unit declares a hard run timeout.

Measured on the operator's Mac on 2026-11-16: one tick started at 08:54Z and never exited. Its
``faulthandler.dump_traceback_later(deadline, exit=True)`` deadline fired mid-tick, then the faulthandler thread spun
in the traceback dump for four hours and the process never ended. launchd starts no second run of a StartInterval job
while one is alive, so overlay refresh was frozen. A watchdog that dumps before it exits depends on the dump finishing;
the tick's watchdog now exits without depending on anything, and the unit declares ``run_timeout_s`` so the host
installer's wrapper ends a run that outlives the tick's own deadline.
"""

from __future__ import annotations

import ast
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units import (
    TIMEOUT_INTERPRETER,
    load_units,
)
from omnimarket.nodes.node_overlay_store_read_effect import runtime_overlay_store_tick
from tests.nodes.node_host_installer_registry_units.test_log_directory import READER
from tests.nodes.node_host_installer_registry_units.test_overlay_store_refresh_workspace_root import (
    brew_python,  # noqa: F401  (the autouse fixture the render below needs)
    render,
)

pytestmark = pytest.mark.unit

TICK_SOURCE = Path(runtime_overlay_store_tick.__file__)

#: How long the blocked tick is given, in the child process.
DEADLINE_S = 2.0
#: What a process that ends at its deadline may add: interpreter teardown, a scheduler hiccup, the report grace.
GRACE_S = 5.0

# The child process runs the real ``main`` with its work blocked and its stderr a pipe nobody reads, already full:
# the case a watchdog that writes before it exits cannot survive. ``main`` is not stubbed beyond the deadline and
# the two calls that would reach a broker.
CHILD = textwrap.dedent(
    """
    import fcntl, os, sys, time

    from omnimarket.nodes.node_overlay_store_read_effect import runtime_overlay_store_tick as tick

    reader, writer = os.pipe()
    flags = fcntl.fcntl(writer, fcntl.F_GETFL)
    fcntl.fcntl(writer, fcntl.F_SETFL, flags | os.O_NONBLOCK)
    try:
        while True:
            os.write(writer, b"x" * 4096)
    except BlockingIOError:
        pass
    fcntl.fcntl(writer, fcntl.F_SETFL, flags)
    os.dup2(writer, 2)

    class Handler:
        async def refresh_all(self):
            time.sleep(600)

    tick.tick_deadline_s = lambda: {deadline}
    tick.load_structured_overlay = lambda *args: object()
    tick.build_handler = lambda *args: Handler()
    sys.stdout.write("ready\\n")
    sys.stdout.flush()
    tick.main(["--omni-home", sys.argv[1], "--state-dir", sys.argv[1]])
    """
)


def test_a_tick_whose_work_blocks_past_the_deadline_exits_within_the_deadline_plus_a_few_seconds(
    tmp_path: Path,
) -> None:
    process = subprocess.Popen(
        [sys.executable, "-c", CHILD.format(deadline=DEADLINE_S), str(tmp_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout is not None
        # Positive control: the tick got as far as its blocked work, so the exit below is the watchdog's.
        assert process.stdout.readline() == "ready\n"
        started = time.monotonic()
        try:
            process.wait(timeout=DEADLINE_S + GRACE_S)
        except subprocess.TimeoutExpired:
            pytest.fail(
                f"the tick was still running {time.monotonic() - started:.1f}s after its work blocked; "
                f"its deadline is {DEADLINE_S}s"
            )
        elapsed = time.monotonic() - started
        assert process.returncode == runtime_overlay_store_tick.EXIT_DEADLINE
        assert elapsed >= DEADLINE_S - 0.5, "the tick ended before its deadline"
    finally:
        process.kill()
        process.wait()
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                stream.close()


def arms_faulthandler_exit(source: str) -> bool:
    """Whether the source calls ``dump_traceback_later`` with a truthy ``exit`` (a call spelled with ``**`` counts)."""
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "attr", getattr(node.func, "id", ""))
        if name != "dump_traceback_later":
            continue
        for keyword in node.keywords:
            if keyword.arg is None:
                return True
            if keyword.arg == "exit" and not (
                isinstance(keyword.value, ast.Constant) and not keyword.value.value
            ):
                return True
    return False


def test_the_detector_sees_a_tree_that_still_arms_the_faulthandler_exit() -> None:
    """Positive control for the check below: the shape the tick had at base is caught."""
    armed = "import faulthandler\nfaulthandler.dump_traceback_later(120.0, exit=True)\n"
    assert arms_faulthandler_exit(armed)
    assert not arms_faulthandler_exit(
        "faulthandler.dump_traceback_later(120.0, exit=False)\n"
    )
    assert not arms_faulthandler_exit("faulthandler.cancel_dump_traceback_later()\n")


def test_no_faulthandler_exit_is_armed_by_the_tick() -> None:
    assert not arms_faulthandler_exit(TICK_SOURCE.read_text(encoding="utf-8"))


def test_the_tick_leaves_no_watchdog_running_when_main_returns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tick run in-process (by a test or any caller) must not leave a process-ending thread behind it."""
    monkeypatch.delenv("ONEX_SKILL_OVERLAY_ROOTS", raising=False)
    exited: list[int] = []
    monkeypatch.setattr(runtime_overlay_store_tick, "tick_deadline_s", lambda: 0.3)
    monkeypatch.setattr(
        runtime_overlay_store_tick, "terminate_process", exited.append, raising=True
    )
    runtime_overlay_store_tick.main(
        ["--omni-home", str(tmp_path), "--state-dir", str(tmp_path / "state")]
    )
    time.sleep(0.8)
    assert exited == []
    assert not [
        thread
        for thread in threading.enumerate()
        if thread.name == runtime_overlay_store_tick.WATCHDOG_THREAD
    ]


def test_the_watchdog_ends_the_process_when_the_deadline_passes_and_not_before() -> (
    None
):
    exited: list[int] = []
    disarm = runtime_overlay_store_tick.arm_watchdog(0.2, exited.append)
    try:
        assert exited == []
        deadline = time.monotonic() + GRACE_S
        while not exited and time.monotonic() < deadline:
            time.sleep(0.05)
    finally:
        disarm()
    assert exited == [runtime_overlay_store_tick.EXIT_DEADLINE]


def test_a_disarmed_watchdog_never_ends_the_process() -> None:
    exited: list[int] = []
    disarm = runtime_overlay_store_tick.arm_watchdog(0.2, exited.append)
    disarm()
    time.sleep(0.6)
    assert exited == []


def test_the_overlay_store_refresh_unit_declares_a_run_timeout_above_the_tick_deadline() -> (
    None
):
    unit = load_units()[READER]
    limit = unit["run_timeout_s"]
    assert isinstance(limit, int)
    assert runtime_overlay_store_tick.tick_deadline_s() < limit
    # Under the interval, so a run the wrapper ends does not hold the next tick back.
    assert limit < unit["schedule"]["start_interval"]


def test_the_rendered_overlay_store_refresh_job_runs_under_the_run_timeout_wrapper(
    tmp_path: Path,
) -> None:
    plist: dict[str, Any] = render(tmp_path)
    arguments: list[str] = plist["ProgramArguments"]
    assert arguments[0] == TIMEOUT_INTERPRETER
    assert arguments[3] == str(load_units()[READER]["run_timeout_s"])
