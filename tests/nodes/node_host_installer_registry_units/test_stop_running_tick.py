# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23349: replacing a tick unit stops or waits for the tick the old unit left running.

Reinstalling ``ai.omninode.job-log-filler`` booted the old job out and loaded the new one, but the old job's tick lived
on (orphaned, parent pid 1) beside the new unit's, both on one state directory. Two causes, two repairs: the run-limit
wrapper put the tick in its own process group, so launchd's TERM at bootout reached the wrapper and not the tick, and the
installer never looked for a tick that outlived the unload. The wrapper now ends its group on TERM, INT and HUP, and
an install or uninstall of a unit that declares ``tick_lock_file`` waits for the lock's holder, then stops it.
"""

from __future__ import annotations

import fcntl
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_host_installer_registry_units.handlers import (
    handler_registry_units as module,
)
from omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units import (
    HandlerRegistryUnits,
)
from tests.nodes.node_host_installer_registry_units.test_registry_units import (
    BREW_PYTHON,
    Recorder,
    _run,
    make_checkout,
    request,
)
from tests.nodes.node_host_installer_registry_units.test_run_timeout import (
    ENTRIES,
    wrapper_of,
)

UNIT = "job-log-filler"
ENTRY = ENTRIES[UNIT]
EVIDENCE = Path(".local/state/omni/job-log-filler")
HOLDER = (
    "import fcntl, os, sys, time\n"
    "h = open(sys.argv[1], 'a+')\n"
    "fcntl.flock(h.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
    "h.seek(0); h.truncate(); h.write(f'pid={os.getpid()}\\n'); h.flush()\n"
    "print('held', flush=True)\n"
    "time.sleep(300)\n"
)


@pytest.fixture(autouse=True)
def brew_python(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        f"{module.__name__}.interpreter_present", lambda path: path == BREW_PYTHON
    )
    monkeypatch.setattr("shutil.which", lambda n: "/usr/bin/uv" if n == "uv" else None)
    monkeypatch.setattr(module, "TICK_GRACE_S", 0.5, raising=False)
    monkeypatch.setattr(module, "TICK_TERM_WAIT_S", 3, raising=False)


def lock_path(tmp_path: Path) -> Path:
    path = tmp_path / "home" / EVIDENCE / "tick.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def spawn_holder(path: Path) -> subprocess.Popen[str]:
    child = subprocess.Popen(
        [sys.executable, "-c", HOLDER, str(path)],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert child.stdout is not None
    assert child.stdout.readline().strip() == "held"
    return child


def install(
    tmp_path: Path, handler_class: Any = HandlerRegistryUnits, mode: str = "install"
) -> tuple[Any, Recorder]:
    branch = tmp_path / "branch"
    if not branch.exists():
        revision = make_checkout(branch, entry=ENTRY)
    else:
        revision = _run(
            ["git", "-C", str(branch), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    run = Recorder()
    result = handler_class(run).handle(
        request(
            tmp_path,
            unit=UNIT,
            mode=mode,
            project_path=str(branch),
            expected_revision=revision,
        )
    )
    return result, run


def free(path: Path) -> bool:
    with path.open("a+") as probe:
        try:
            fcntl.flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True


# ------------------------------------------------------------------ the wrapper ends its group on TERM


@pytest.mark.parametrize("sent", [signal.SIGTERM, signal.SIGINT, signal.SIGHUP])
def test_a_signal_to_the_wrapper_ends_the_tick_in_its_process_group(
    tmp_path: Path, sent: signal.Signals
) -> None:
    wrapper, _ = wrapper_of(tmp_path / "render", UNIT)
    child = tmp_path / "child.pid"
    started = subprocess.Popen(
        [*wrapper[:3], "300", "/bin/sh", "-c", f"sleep 300 & echo $! > {child}; wait"]
    )
    for _ in range(100):
        if child.exists() and child.read_text().strip():
            break
        time.sleep(0.1)
    pid = int(child.read_text())
    started.send_signal(sent)
    code = started.wait(timeout=30)
    for _ in range(50):
        alive = subprocess.run(
            ["kill", "-0", str(pid)], capture_output=True, check=False
        ).returncode
        if alive != 0:
            break
        time.sleep(0.1)
    assert alive != 0, "the tick outlived the wrapper that launchd stopped"
    assert code == 128 + sent.value


# ------------------------------------------------------------------ the installer waits for, then stops, the tick


def test_a_tick_that_ends_inside_the_grace_is_waited_for_and_not_signalled(
    tmp_path: Path,
) -> None:
    path = lock_path(tmp_path)
    holder = spawn_holder(path)
    sent: list[int] = []

    class Waits(HandlerRegistryUnits):
        def signal(self, pid: int, sig: int) -> None:
            sent.append(sig)
            super().signal(pid, sig)

    try:
        started = time.monotonic()
        # the old tick finishes by itself 0.2 s into the install
        subprocess.Popen(["/bin/sh", "-c", f"sleep 0.2; kill -9 {holder.pid}"])
        result, _ = install(tmp_path, Waits)
        assert result.status == "success", result.stderr
        assert time.monotonic() - started < 10
    finally:
        holder.kill()
        holder.wait()
    assert sent == []
    assert free(path)


def test_a_tick_that_outlives_the_grace_is_stopped_before_the_new_unit_loads(
    tmp_path: Path,
) -> None:
    path = lock_path(tmp_path)
    holder = spawn_holder(path)
    at_bootstrap: list[bool] = []

    class Checks(Recorder):
        def __call__(self, argv: list[str]) -> Any:
            if argv[:2] == ["launchctl", "bootstrap"]:
                at_bootstrap.append(free(path))
            return super().__call__(argv)

    branch = tmp_path / "branch"
    revision = make_checkout(branch, entry=ENTRY)
    try:
        result = HandlerRegistryUnits(Checks()).handle(
            request(
                tmp_path,
                unit=UNIT,
                mode="install",
                project_path=str(branch),
                expected_revision=revision,
            )
        )
        assert result.status == "success", result.stderr
        assert holder.wait(timeout=10) == -signal.SIGTERM
    finally:
        holder.kill()
        holder.wait()
    assert at_bootstrap == [True]
    assert "stopped the running tick" in result.stdout


def test_a_tick_that_will_not_stop_refuses_the_install_and_loads_nothing(
    tmp_path: Path,
) -> None:
    path = lock_path(tmp_path)
    holder = spawn_holder(path)

    class Deaf(HandlerRegistryUnits):
        def signal(self, pid: int, sig: int) -> None:
            return None

    try:
        result, run = install(tmp_path, Deaf)
    finally:
        holder.kill()
        holder.wait()
    assert result.status == "failure"
    assert "did not stop" in result.stderr
    assert str(path) in result.stderr
    assert [c[1] for c in run.calls if c[0] == "launchctl"] == ["bootout"]


def test_an_uninstall_stops_the_running_tick_too(tmp_path: Path) -> None:
    path = lock_path(tmp_path)
    holder = spawn_holder(path)
    try:
        result, _ = install(tmp_path, mode="uninstall")
        assert result.status == "success", result.stderr
        assert holder.wait(timeout=10) == -signal.SIGTERM
    finally:
        holder.kill()
        holder.wait()


def test_a_unit_with_no_tick_lock_and_a_free_lock_install_as_before(
    tmp_path: Path,
) -> None:
    result, run = install(tmp_path)
    assert result.status == "success", result.stderr
    assert [c[1] for c in run.calls if c[0] == "launchctl"] == ["bootout", "bootstrap"]
