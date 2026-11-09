# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Subprocess backend hardening of the host storage effect (OMN-22906, Tessl review findings).

Each test runs the real subprocess backend against a real child process: remote-shell argument
boundaries, undecodable output, an unbounded output volume, a timeout that leaves descendants, and
an after-reading that cannot be taken.
"""

from __future__ import annotations

import sys
import time
import tracemalloc
from pathlib import Path

import pytest

from omnimarket.nodes.node_host_storage_effect.handlers.backend_subprocess import (
    BackendHostStorageSubprocess,
)
from omnimarket.nodes.node_host_storage_effect.handlers.handler_host_storage import (
    HandlerHostStorage,
)
from omnimarket.nodes.node_host_storage_effect.models.model_host_storage import (
    EnumHostStorageOutcome,
    ModelHostStorageRequest,
    ModelHostStorageRun,
)
from omnimarket.nodes.node_host_storage_effect.models.model_host_storage_overlay import (
    ModelHostStorageOverlay,
    ModelStorageCommand,
    ModelStorageHost,
)

pytestmark = pytest.mark.unit

# Behaves like ssh: the words after the transport words are joined with spaces and handed to a shell.
FAKE_REMOTE_SHELL = (
    "import subprocess,sys;"
    "sys.exit(subprocess.run(' '.join(sys.argv[1:]),shell=True).returncode)"
)


def _command(
    argv: list[str], measured: str, timeout: float = 10
) -> ModelStorageCommand:
    return ModelStorageCommand(
        name="probe_cmd",
        argv=tuple(argv),
        measured_path=measured,
        timeout_seconds=timeout,
    )


def _host(prefix: list[str]) -> ModelStorageHost:
    return ModelStorageHost(
        name="store-host", transport_prefix=tuple(prefix), commands=("probe_cmd",)
    )


def _backend() -> BackendHostStorageSubprocess:
    return BackendHostStorageSubprocess(
        (sys.executable, "-c", "print(1)", "{path}"), 10
    )


def test_remote_transport_keeps_the_declared_argument_boundaries(
    tmp_path: Path,
) -> None:
    """A declared argument with a space or a shell metacharacter reaches the command literally."""
    target = tmp_path / "dir with space" / "file;touch injected"
    target.parent.mkdir()
    writer = "import sys;open(sys.argv[1],'w').write('x')"
    host = _host([sys.executable, "-c", FAKE_REMOTE_SHELL])
    run = _backend().run(
        host, _command([sys.executable, "-c", writer, str(target)], str(tmp_path))
    )
    assert run.exit_code == 0, run.output_tail
    assert target.read_text() == "x"
    assert not (tmp_path / "dir with space" / "injected").exists()
    assert not (Path.cwd() / "injected").exists()


def test_remote_transport_keeps_the_measured_path_literal(tmp_path: Path) -> None:
    spaced = tmp_path / "a b"
    spaced.mkdir()
    reader = (
        "import os,sys;"
        "sys.stdout.write(str(100 if os.path.isdir(sys.argv[1]) else 'x'))"
    )
    backend = BackendHostStorageSubprocess((sys.executable, "-c", reader, "{path}"), 10)
    host = _host([sys.executable, "-c", FAKE_REMOTE_SHELL])
    assert backend.free_bytes(host, str(spaced)) == 100


def test_output_that_is_not_text_keeps_the_exit_status(tmp_path: Path) -> None:
    emit = "import sys;sys.stdout.buffer.write(b'\\xff\\xfe done');sys.exit(0)"
    run = _backend().run(
        _host([]), _command([sys.executable, "-c", emit], str(tmp_path))
    )
    assert run.exit_code == 0
    assert "done" in run.output_tail


def test_a_reading_with_undecodable_bytes_is_still_read(tmp_path: Path) -> None:
    emit = "import sys;sys.stdout.buffer.write(b'\\xff\\xfe\\n12345\\n')"
    backend = BackendHostStorageSubprocess((sys.executable, "-c", emit, "{path}"), 10)
    assert backend.free_bytes(_host([]), str(tmp_path)) == 12345


def test_retained_output_stays_bounded_while_the_command_runs(tmp_path: Path) -> None:
    flood = (
        "import sys\n"
        "chunk = b'y' * 65536\n"
        "for _ in range(480):\n"
        "    sys.stdout.buffer.write(chunk)\n"
        "sys.stdout.buffer.write(b'END')\n"
    )
    tracemalloc.start()
    try:
        run = _backend().run(
            _host([]), _command([sys.executable, "-c", flood], str(tmp_path))
        )
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert run.exit_code == 0
    assert run.output_tail.endswith("END")
    assert peak < 4 * 1024 * 1024, (
        f"peak traced memory {peak} bytes for 30 MiB of output"
    )


def test_a_timeout_ends_the_descendants_of_the_command(tmp_path: Path) -> None:
    marker = tmp_path / "helper-finished"
    helper = f"import time;time.sleep(2);open({str(marker)!r},'w').write('x')"
    parent = (
        "import subprocess,sys,time;"
        f"subprocess.Popen([sys.executable,'-c',{helper!r}],"
        "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);"
        "time.sleep(60)"
    )
    started = time.monotonic()
    run = _backend().run(
        _host([]), _command([sys.executable, "-c", parent], str(tmp_path), timeout=0.6)
    )
    assert run.timed_out is True
    assert time.monotonic() - started < 10
    time.sleep(3)
    assert not marker.exists(), (
        "a helper the command started kept running after the timeout"
    )


class _Backend:
    """A backend whose free-bytes readings are scripted: the command itself exits zero."""

    def __init__(self, readings: list[int | None]) -> None:
        self._readings = list(readings)

    def free_bytes(self, host: ModelStorageHost, path: str) -> int | None:
        return self._readings.pop(0)

    def run(
        self, host: ModelStorageHost, command: ModelStorageCommand
    ) -> ModelHostStorageRun:
        return ModelHostStorageRun(exit_code=0)


def _overlay() -> ModelHostStorageOverlay:
    return ModelHostStorageOverlay(
        backend="in_memory",
        free_bytes_argv=("x", "{path}"),
        free_bytes_timeout_seconds=1,
        commands=(_command(["x"], "/p"),),
        hosts=(_host([]),),
    )


def test_a_missing_after_reading_is_not_an_ordinary_success() -> None:
    handler = HandlerHostStorage(_overlay(), _Backend([100, None]))
    result = handler.handle(
        ModelHostStorageRequest(
            host="store-host", command="probe_cmd", requested_by="t"
        )
    )
    assert result.outcome is not EnumHostStorageOutcome.OK
    assert result.outcome is EnumHostStorageOutcome.COMPLETED_UNOBSERVED
    assert result.exit_code == 0
    assert result.free_bytes_before == 100
    assert result.free_bytes_after is None
    assert result.detail is not None
    assert "after" in result.detail


def test_a_taken_after_reading_stays_an_ordinary_success() -> None:
    handler = HandlerHostStorage(_overlay(), _Backend([100, 150]))
    result = handler.handle(
        ModelHostStorageRequest(
            host="store-host", command="probe_cmd", requested_by="t"
        )
    )
    assert result.outcome is EnumHostStorageOutcome.OK
    assert result.freed_bytes == 50
