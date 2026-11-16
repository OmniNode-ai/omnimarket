# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-24839: the overlay store refresh tick ends at its deadline, and its unit declares a hard run timeout.

Measured on the operator's Mac on 2026-11-16: one tick started at 08:54Z and never exited. Its
``faulthandler.dump_traceback_later(deadline, exit=True)`` deadline fired mid-tick, then the faulthandler thread stayed
in the traceback dump for four hours and the process never ended. launchd starts no second run of a StartInterval
job while one is alive, so overlay refresh was frozen. A watchdog that dumps before it exits depends on the dump finishing;
the tick's watchdog now exits without depending on anything, and the unit declares ``run_timeout_s`` so the host
installer's wrapper ends a run that outlives the tick's own deadline.
"""

from __future__ import annotations

import ast
import re
import select
import subprocess
import sys
import textwrap
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
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
CONTRACT = Path(__file__).resolve().parents[3] / "contracts" / "OMN-24839.yaml"
LEDGER_CHECK = "dod-overlay-tick-ledger-rows-ac4"
#: The file argument a ledger check ends with: a quoted environment variable.
LEDGER_FILE_ARGUMENT = re.compile(r'"\$([A-Z_]+)"\s*$')
LANE = "ovwatch-1420"
#: The four other ticks that arm the faulthandler exit; the lane's STATUS rows cite each by file name.
OTHER_TICK_FILES = (
    "runtime_c32_probe_tick.py",
    "runtime_declared_clone_tick.py",
    "runtime_job_log_filler_tick.py",
    "runtime_overlay_store_load_tick.py",
)
#: The text of the lane's two rows as the real ledger holds them, shortened after the sentence the checks read.
TICKS_TEXT = (
    "other node ticks that arm the faulthandler exit watchdog (dump_traceback_later with exit=True), read at base via "
    "git grep over src, changed in none of them by this lane: {files}"
)
PRODUCER_TEXT = (
    "producer of the stale overlay readings (read only, changed nothing): the reading age is now minus the snapshot "
    "stamp's loaded_at, and loaded_at is set by node_overlay_store_load_effect (run by the overlay-store-load-systemd "
    "unit, module runtime_overlay_store_load_tick)."
)
TICK_PATHS_BY_FILE = {
    "runtime_c32_probe_tick.py": "src/omnimarket/nodes/node_c32_auth_boundary_probe_effect/runtime_c32_probe_tick.py",
    "runtime_declared_clone_tick.py": "src/omnimarket/nodes/node_canonical_clone_refresh_effect/runtime_declared_clone_tick.py",
    "runtime_job_log_filler_tick.py": "src/omnimarket/nodes/node_job_log_filler_effect/runtime_job_log_filler_tick.py",
    "runtime_overlay_store_load_tick.py": "src/omnimarket/nodes/node_overlay_store_load_effect/runtime_overlay_store_load_tick.py",
}

#: How long the blocked tick is given, in the child process.
DEADLINE_S = 2.0
#: What a process that ends at its deadline may add: interpreter teardown, a scheduler hiccup, the report grace.
GRACE_S = 5.0
#: How long a child is given to reach its blocked work and print ``ready``; a child that hangs before it fails the test.
READY_S = 30.0

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
            # Printed from inside the tick's own work, so "ready" means the tick reached the call that blocks.
            # ``started`` is read just before ``main`` arms the watchdog, so the deadline cannot pass before it.
            sys.stdout.write(f"ready {{started!r}}\\n")
            sys.stdout.flush()
            time.sleep(600)

    tick.tick_deadline_s = lambda: {deadline}
    tick.load_structured_overlay = lambda *args: object()
    tick.build_handler = lambda *args: Handler()
    started = time.monotonic()
    tick.main(["--omni-home", sys.argv[1], "--state-dir", sys.argv[1]])
    """
)


def read_first_line(process: subprocess.Popen[str], timeout_s: float) -> str:
    """The first line the child prints, or a test failure when it prints none within ``timeout_s``.

    ``readline`` alone has no timeout, so a child that hangs before it prints ``ready`` would hang the test."""
    assert process.stdout is not None
    readable, _, _ = select.select([process.stdout], [], [], timeout_s)
    if not readable:
        pytest.fail(f"the child never printed a line within {timeout_s:g}s")
    return process.stdout.readline()


def test_a_child_that_hangs_before_ready_fails_the_read_in_place_of_hanging_the_test() -> (
    None
):
    """The child never prints ``ready``: the read of its first line gives up at its bound and fails the test."""
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(600)"],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        started = time.monotonic()
        with pytest.raises(pytest.fail.Exception, match="never printed"):
            read_first_line(child, 0.5)
        assert time.monotonic() - started < GRACE_S
    finally:
        child.kill()
        child.wait()
        if child.stdout is not None:
            child.stdout.close()


def test_a_child_that_prints_before_the_bound_gives_its_line() -> None:
    """Positive control for the bound: a child that answers is read, so the failure above is the missing line."""
    child = subprocess.Popen(
        [sys.executable, "-c", "print('ready 1.5', flush=True)"],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert read_first_line(child, READY_S) == "ready 1.5\n"
    finally:
        child.wait()
        if child.stdout is not None:
            child.stdout.close()


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
        ready, _, clock = read_first_line(process, READY_S).partition(" ")
        assert ready == "ready"
        # ``time.monotonic`` is one clock for every process on the host, so the child's reading is comparable here.
        started = float(clock)
        try:
            process.wait(timeout=DEADLINE_S + GRACE_S)
        except subprocess.TimeoutExpired:
            pytest.fail(
                f"the tick was still running {time.monotonic() - started:.1f}s after it started; "
                f"its deadline is {DEADLINE_S}s"
            )
        elapsed = time.monotonic() - started
        assert process.returncode == runtime_overlay_store_tick.EXIT_DEADLINE
        # The stderr pipe is full, so the report blocks and the watchdog spends its whole grace before it ends the
        # process: the earliest honest exit is the deadline plus the grace, and a watchdog that fired early misses it.
        earliest = DEADLINE_S + runtime_overlay_store_tick.REPORT_GRACE_S - 0.05
        assert elapsed >= earliest, (
            "the tick ended before its deadline and its report grace"
        )
    finally:
        process.kill()
        process.wait()
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                stream.close()


def is_falsy_constant(node: ast.expr) -> bool:
    return isinstance(node, ast.Constant) and not node.value


def arms_faulthandler_exit(source: str) -> bool:
    """Whether the source calls ``dump_traceback_later`` in a way that can pass a truthy ``exit``.

    ``exit`` is the fourth positional of ``dump_traceback_later(timeout, repeat, file, exit)`` and a keyword. A call
    spelled with ``**``, with a starred argument, or with a fourth positional that is not a false constant counts."""
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "attr", getattr(node.func, "id", ""))
        if name != "dump_traceback_later":
            continue
        if any(isinstance(arg, ast.Starred) for arg in node.args):
            return True
        if len(node.args) >= 4 and not is_falsy_constant(node.args[3]):
            return True
        for keyword in node.keywords:
            if keyword.arg is None:
                return True
            if keyword.arg == "exit" and not is_falsy_constant(keyword.value):
                return True
    return False


def imports_faulthandler(source: str) -> bool:
    """Whether the source imports ``faulthandler`` in any spelling (``import``, ``from``, an alias)."""
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import) and any(
            alias.name.split(".")[0] == "faulthandler" for alias in node.names
        ):
            return True
        if (
            isinstance(node, ast.ImportFrom)
            and (node.module or "").split(".")[0] == "faulthandler"
        ):
            return True
    return False


@pytest.mark.parametrize(
    "armed",
    [
        "faulthandler.dump_traceback_later(120.0, exit=True)\n",
        "faulthandler.dump_traceback_later(120, False, sys.stderr, True)\n",
        "faulthandler.dump_traceback_later(120, False, sys.stderr, flag)\n",
        "faulthandler.dump_traceback_later(*args)\n",
        "faulthandler.dump_traceback_later(120, **kwargs)\n",
        "faulthandler.dump_traceback_later(120.0, exit=flag)\n",
        "dump_traceback_later(120.0, exit=True)\n",
    ],
)
def test_the_detector_sees_a_tree_that_still_arms_the_faulthandler_exit(
    armed: str,
) -> None:
    """Positive controls for the check below: the shape the tick had at base, and each spelling that hides ``exit``."""
    assert arms_faulthandler_exit(armed)


@pytest.mark.parametrize(
    "unarmed",
    [
        "faulthandler.dump_traceback_later(120.0, exit=False)\n",
        "faulthandler.dump_traceback_later(120.0)\n",
        "faulthandler.dump_traceback_later(120, False, sys.stderr, False)\n",
        "faulthandler.cancel_dump_traceback_later()\n",
    ],
)
def test_the_detector_passes_a_call_that_cannot_exit_the_process(
    unarmed: str,
) -> None:
    assert not arms_faulthandler_exit(unarmed)


def test_the_import_detector_sees_every_spelling_of_a_faulthandler_import() -> None:
    for source in (
        "import faulthandler\n",
        "import faulthandler as fh\n",
        "from faulthandler import dump_traceback_later\n",
        "from faulthandler import dump_traceback_later as arm\n",
    ):
        assert imports_faulthandler(source), source
    assert not imports_faulthandler("import threading\nfrom os import _exit\n")


def test_no_faulthandler_exit_is_armed_by_the_tick() -> None:
    source = TICK_SOURCE.read_text(encoding="utf-8")
    assert not arms_faulthandler_exit(source)
    # The tick has no use for the module at all, so an aliased import cannot hide a call the detector would miss.
    assert not imports_faulthandler(source)


def watchdog_threads() -> set[threading.Thread]:
    return {
        thread
        for thread in threading.enumerate()
        if thread.name == runtime_overlay_store_tick.WATCHDOG_THREAD
    }


def arm(
    deadline_s: float, terminate: Callable[[int], None]
) -> tuple[Callable[[], None], threading.Thread]:
    """Arm the watchdog and return its disarm call with the thread it started."""
    before = watchdog_threads()
    disarm = runtime_overlay_store_tick.arm_watchdog(deadline_s, terminate)
    (thread,) = watchdog_threads() - before
    return disarm, thread


def test_the_tick_leaves_no_watchdog_running_when_main_returns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tick run in-process (by a test or any caller) must not leave a process-ending thread behind it.

    The loader is stubbed to refuse, so ``main`` returns at once whatever the host's overlay sources hold, and the
    deadline is long: a watchdog that was not disarmed is still alive when the thread is joined."""

    def refuse(*_: object) -> None:
        raise NodeOverlayError("no overlay in this test")

    exited: list[int] = []
    monkeypatch.setattr(runtime_overlay_store_tick, "tick_deadline_s", lambda: 60.0)
    monkeypatch.setattr(runtime_overlay_store_tick, "load_structured_overlay", refuse)
    monkeypatch.setattr(
        runtime_overlay_store_tick, "terminate_process", exited.append, raising=True
    )
    before = watchdog_threads()
    code = runtime_overlay_store_tick.main(
        ["--omni-home", str(tmp_path), "--state-dir", str(tmp_path / "state")]
    )
    assert code == runtime_overlay_store_tick.EXIT_UNREADABLE
    for thread in watchdog_threads() - before:
        thread.join(GRACE_S)
        assert not thread.is_alive(), "main returned with its watchdog still armed"
    assert exited == []


def test_the_watchdog_ends_the_process_when_the_deadline_passes_and_not_before() -> (
    None
):
    exited: list[int] = []
    ended_at: list[float] = []

    def terminate(status: int) -> None:
        exited.append(status)
        ended_at.append(time.monotonic())

    started = time.monotonic()
    disarm, thread = arm(0.2, terminate)
    try:
        assert exited == []
        thread.join(GRACE_S)
    finally:
        disarm()
    assert exited == [runtime_overlay_store_tick.EXIT_DEADLINE]
    assert ended_at[0] - started >= 0.2, "the watchdog ended the process early"


def test_a_disarmed_watchdog_never_ends_the_process() -> None:
    exited: list[int] = []
    disarm, thread = arm(0.2, exited.append)
    disarm()
    thread.join(GRACE_S)
    assert not thread.is_alive()
    assert exited == []


class RefusedStart(threading.Thread):
    """A thread that cannot start: what ``Thread.start`` does when the host is out of threads."""

    def start(self) -> None:
        raise RuntimeError("can't start new thread")


class Interrupt(
    BaseException
):  # not an Exception, like KeyboardInterrupt or SystemExit
    """An error that ``contextlib.suppress(Exception)`` does not catch."""


class InterruptedStart(threading.Thread):
    """A thread whose ``start`` raises a ``BaseException`` that is not an ``Exception``."""

    def start(self) -> None:
        raise Interrupt("interrupted while starting the report thread")


def threads_for(report_thread: type[threading.Thread]) -> type:
    """Stands in for the tick's ``threading`` module: the watchdog thread is real, the report thread is ``report_thread``."""

    class ThreadsFor:
        Event = threading.Event
        get_ident = staticmethod(threading.get_ident)

        @staticmethod
        def Thread(*args: Any, **kwargs: Any) -> threading.Thread:  # noqa: N802  (the class it replaces)
            if kwargs.get("name") == runtime_overlay_store_tick.WATCHDOG_THREAD:
                return threading.Thread(*args, **kwargs)
            return report_thread(*args, **kwargs)

    return ThreadsFor


def refuse_report(*_: object) -> None:
    raise OSError("stderr is gone")


# The raising report runs in a thread of its own, and its error reaches the thread excepthook by design.
@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
@pytest.mark.parametrize(
    ("report_raises", "thread_start_raises"),
    [(True, False), (False, True), (True, True)],
    ids=["report-raises", "thread-start-raises", "both-raise"],
)
def test_the_watchdog_ends_the_process_even_when_its_report_fails(
    monkeypatch: pytest.MonkeyPatch, report_raises: bool, thread_start_raises: bool
) -> None:
    """The report is a courtesy: whatever it does, the process ends.

    Each of these failures is an ``Exception`` that the report's ``suppress`` swallows on its own, so none of them
    needs the ``finally``; the next test is the one that does."""
    if report_raises:
        monkeypatch.setattr(runtime_overlay_store_tick, "_report_stuck", refuse_report)
    if thread_start_raises:
        monkeypatch.setattr(
            runtime_overlay_store_tick, "threading", threads_for(RefusedStart)
        )
    exited: list[int] = []
    disarm, thread = arm(0.1, exited.append)
    try:
        thread.join(GRACE_S)
    finally:
        disarm()
    assert exited == [runtime_overlay_store_tick.EXIT_DEADLINE]


# The watchdog thread dies with the ``Interrupt`` after it has ended the process, and its error reaches the thread
# excepthook by design.
@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_the_watchdog_ends_the_process_when_the_report_thread_start_raises_a_base_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the ``finally`` ends the process here: ``suppress(Exception)`` does not catch a ``BaseException``.

    Without the ``finally`` the ``Interrupt`` leaves the watchdog thread before it reaches ``terminate`` and
    ``exited`` stays empty."""
    monkeypatch.setattr(
        runtime_overlay_store_tick, "threading", threads_for(InterruptedStart)
    )
    exited: list[int] = []
    disarm, thread = arm(0.1, exited.append)
    try:
        thread.join(GRACE_S)
    finally:
        disarm()
    assert not thread.is_alive()
    assert exited == [runtime_overlay_store_tick.EXIT_DEADLINE]


def test_a_tick_that_returns_during_the_report_grace_is_not_ended(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The tick finishes just after the deadline, while the report is still blocked: it is not killed with 124."""
    reporting = threading.Event()
    release = threading.Event()

    def blocked_report(*_: object) -> None:
        reporting.set()
        release.wait(GRACE_S * 4)

    monkeypatch.setattr(runtime_overlay_store_tick, "_report_stuck", blocked_report)
    exited: list[int] = []
    disarm, thread = arm(0.1, exited.append)
    try:
        assert reporting.wait(GRACE_S), "the watchdog never reached its report"
        disarm()
        thread.join(runtime_overlay_store_tick.REPORT_GRACE_S + GRACE_S)
        assert not thread.is_alive()
    finally:
        release.set()
        disarm()
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


def ledger_row(kind: str, lane: str, text: str, request: int = 1) -> str:
    """A row in the cells the ledger writes (see ``REAL_ROW_SHAPE``), with the placeholders a fixture needs."""
    return (
        f"2026-11-16T13:20:0{request}Z | {kind} | lane={lane} | principal=fixture-principal | ticket=OMN-24839 "
        f"| actor=claude:sonnet:{lane} | model=claude-sonnet-5-5 | via=relay:h202 "
        f"| req=00000000-0000-4000-8000-00000000000{request} | via=bus:h200 | {text}"
    )


def good_ledger(
    tick_files: tuple[str, ...] = OTHER_TICK_FILES,
    kind: str = "STATUS",
    lane: str = LANE,
) -> list[str]:
    files = ", ".join(TICK_PATHS_BY_FILE[name] for name in tick_files)
    return [
        ledger_row(kind, lane, TICKS_TEXT.format(files=files), 1),
        ledger_row(kind, lane, PRODUCER_TEXT, 2),
    ]


def contract_checks() -> tuple[list[str], list[str]]:
    """The ledger checks of the contract entry, and its other checks (they read the tree, not the ledger)."""
    entry = next(
        item
        for item in yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))["dod_evidence"]
        if item["id"] == LEDGER_CHECK
    )
    commands = [check["check_value"] for check in entry["checks"]]
    on_ledger = [
        command for command in commands if LEDGER_FILE_ARGUMENT.search(command)
    ]
    return on_ledger, [command for command in commands if command not in on_ledger]


def ledger_check_results(tmp_path: Path, rows: list[str]) -> list[bool]:
    ledger = tmp_path / "ledger.md"
    ledger.write_text("\n".join(rows) + "\n", encoding="utf-8")
    on_ledger, _ = contract_checks()
    results = []
    for command in on_ledger:
        # The variable the command reads its file from is the command's own; the test only points it at a fixture.
        match = LEDGER_FILE_ARGUMENT.search(command)
        assert match is not None
        env = {match.group(1): str(ledger), "PATH": "/usr/bin:/bin"}
        results.append(
            subprocess.run(command, shell=True, env=env, capture_output=True).returncode
            == 0
        )
    return results


#: The cells the ledger writes on a lane's STATUS row, in order (read from the lane's rows of 2026-11-16 on the real
#: ledger): the time, the type, the lane, the principal, the ticket, the executor, the model, the relay, the request
#: id, the bus, then the text. A fixture of any other shape is a format the ledger does not write.
REAL_ROW_SHAPE = re.compile(
    r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ \| STATUS \| lane=[\w-]+ \| principal=[\w-]+ \| ticket=OMN-\d+ "
    r"\| actor=\S+ \| model=\S+ \| via=relay:\w+ \| req=[0-9a-f-]{36} \| via=bus:\w+ \| \S.*$"
)


def test_the_fixture_rows_are_in_the_shape_the_real_ledger_writes() -> None:
    """The ledger tests below run the contract's greps on these rows, so the rows must be a row the ledger writes."""
    rows = good_ledger()
    assert len(rows) == 2
    for row in rows:
        assert REAL_ROW_SHAPE.match(row), row


def test_the_ledger_check_has_one_command_per_tick_file_and_one_for_the_producer() -> (
    None
):
    on_ledger, _ = contract_checks()
    assert len(on_ledger) == len(OTHER_TICK_FILES) + 1
    for name in OTHER_TICK_FILES:
        assert len([c for c in on_ledger if name.replace(".", r"\.") in c]) == 1, name
    assert len([c for c in on_ledger if "producer of the stale" in c]) == 1


def test_the_ledger_check_passes_on_rows_that_cite_every_tick_file_and_the_producer(
    tmp_path: Path,
) -> None:
    """Positive control: the check is satisfiable, so each failure below is the row it was missing."""
    assert all(ledger_check_results(tmp_path, good_ledger()))


@pytest.mark.parametrize("dropped", OTHER_TICK_FILES)
def test_the_ledger_check_fails_when_one_tick_file_is_missing_from_the_rows(
    tmp_path: Path, dropped: str
) -> None:
    kept = tuple(name for name in OTHER_TICK_FILES if name != dropped)
    results = ledger_check_results(tmp_path, good_ledger(kept))
    assert results.count(False) == 1, results


def test_the_ledger_check_fails_without_the_producer_row(tmp_path: Path) -> None:
    results = ledger_check_results(tmp_path, good_ledger()[:1])
    assert results.count(False) == 1, results


@pytest.mark.parametrize(
    ("kind", "lane"), [("MSG", LANE), ("STATUS", "some-other-lane")]
)
def test_the_ledger_check_ignores_rows_of_another_lane_or_another_row_type(
    tmp_path: Path, kind: str, lane: str
) -> None:
    rows = good_ledger(kind=kind, lane=lane)
    assert not any(ledger_check_results(tmp_path, rows))


def test_the_tree_check_fails_when_another_file_arms_dump_traceback_later(
    tmp_path: Path,
) -> None:
    """The check that no other tick arms the timer, run in a scratch tree: clean passes, an extra file fails."""
    _, tree_checks = contract_checks()
    (command,) = [c for c in tree_checks if "grep -rl" in c]
    src = tmp_path / "src" / "nodes"
    for name in OTHER_TICK_FILES:
        (src / name.replace(".py", "")).mkdir(parents=True)
    layout = {
        "node_c32_auth_boundary_probe_effect/runtime_c32_probe_tick.py",
        "node_canonical_clone_refresh_effect/runtime_declared_clone_tick.py",
        "node_job_log_filler_effect/runtime_job_log_filler_tick.py",
        "node_overlay_store_load_effect/runtime_overlay_store_load_tick.py",
    }
    for relative in layout:
        target = src / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("faulthandler.dump_traceback_later(1, exit=True)\n")

    def run() -> int:
        return subprocess.run(
            command, shell=True, cwd=tmp_path, env={"PATH": "/usr/bin:/bin"}
        ).returncode

    assert run() == 0
    extra = src / "node_other" / "runtime_other_tick.py"
    extra.parent.mkdir()
    extra.write_text("faulthandler.dump_traceback_later(1, exit=True)\n")
    assert run() != 0


def test_the_tree_check_fails_where_there_is_no_src_tree(tmp_path: Path) -> None:
    """An absent ``src`` makes the listing empty, which must not read as "no other file arms the timer"."""
    _, tree_checks = contract_checks()
    (command,) = [c for c in tree_checks if "grep -rl" in c]
    result = subprocess.run(
        command,
        shell=True,
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin"},
        capture_output=True,
        check=False,
    )
    assert result.returncode != 0


REPO_ROOT = Path(__file__).resolve().parents[3]
#: The files the ledger rows cite and the tree check excludes, as the contract's tree check spells them.
OTHER_TICK_PATHS = {
    "src/omnimarket/nodes/node_c32_auth_boundary_probe_effect/runtime_c32_probe_tick.py",
    "src/omnimarket/nodes/node_canonical_clone_refresh_effect/runtime_declared_clone_tick.py",
    "src/omnimarket/nodes/node_job_log_filler_effect/runtime_job_log_filler_tick.py",
    "src/omnimarket/nodes/node_overlay_store_load_effect/runtime_overlay_store_load_tick.py",
}


def test_the_real_tree_has_exactly_the_four_other_ticks_arming_dump_traceback_later() -> (
    None
):
    """The contract's own tree command, run from the repo root on the real ``src`` tree, not on a scratch copy.

    The listing is asserted whole, so a fifth file arming the timer fails here even where the contract's command
    would be narrowed; the contract command must pass on the same tree."""
    _, tree_checks = contract_checks()
    (command,) = [c for c in tree_checks if "grep -rl" in c]
    listing = subprocess.run(
        ["grep", "-rl", "--include=*.py", "dump_traceback_later", "src/"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    # grep exits 1 for no match and 2 for an error; neither is a listing.
    assert listing.returncode == 0, listing.stderr
    assert set(listing.stdout.split()) == OTHER_TICK_PATHS
    # The files are real (positive control: the listing is not of an empty or moved tree).
    assert all((REPO_ROOT / path).is_file() for path in OTHER_TICK_PATHS)
    assert (
        subprocess.run(command, shell=True, cwd=REPO_ROOT, check=False).returncode == 0
    )
