# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_lane_process_effect: the posix adapter, the scripted adapter, the handler and the G-NODE gate."""

from __future__ import annotations

import contextlib
import importlib
import os
import re
import signal
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, cast

import pytest
import yaml
from omnibase_spi.contracts.services import (
    ModelLaneProcessRunResult,
    ModelLaneProcessStartResult,
    ModelLaneStartSpec,
)
from omnibase_spi.protocols.lane import ProtocolLaneProcessRunner
from pydantic import ValidationError

from omnimarket.nodes.node_lane_process_effect.adapters import (
    AdapterPosixProcess,
    AdapterScriptedRunner,
)
from omnimarket.nodes.node_lane_process_effect.handlers.handler_lane_process import (
    HandlerLaneProcess,
)
from omnimarket.nodes.node_lane_process_effect.models import (
    EnumLaneProcessOperation,
    EnumLaneProcessOutcome,
    ModelLaneProcessRequest,
)
from omnimarket.nodes.node_lane_process_effect.overlay import ModelLaneProcessOverlay
from omnimarket.nodes.node_lane_process_effect.tests.support import (
    LANE,
    FakeContainer,
    alive,
    handler_with,
    python_spec,
    spec_for,
    wait_for,
    write_script,
)

if TYPE_CHECKING:
    from omnibase_core.container import ModelONEXContainer

pytestmark = pytest.mark.unit

NODE_DIR = Path(__file__).resolve().parents[1]
LONG_SLEEP = "import time; time.sleep(60)"


@pytest.fixture
def stray() -> Iterator[list[int]]:
    """Pids of started runners; each process group is ended when the test ends."""
    pids: list[int] = []
    yield pids
    for pid in pids:
        # A group that already ended is gone (a zombie answers EPERM on macOS).
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pid, signal.SIGKILL)


@pytest.fixture
def adapter(tmp_path: Path) -> AdapterPosixProcess:
    return AdapterPosixProcess(str(tmp_path / "out"))


def log_text(adapter_dir: Path, lane: str = LANE) -> str:
    path = adapter_dir / f"{lane}.log"
    return path.read_text(encoding="utf-8") if path.exists() else ""


# ------------------------------------------------------------------ AC1: start_detached


def test_start_detached_returns_early(
    tmp_path: Path, adapter: AdapterPosixProcess, stray: list[int]
) -> None:
    marker = tmp_path / "marker"
    script = write_script(
        tmp_path,
        "leader.py",
        f"""
        import pathlib, sys, time
        pathlib.Path({str(marker)!r}).write_text("ran")
        print("leader up", flush=True)
        time.sleep(60)
        """,
    )
    result = adapter.start_detached(python_spec(script, tmp_path))
    assert result.state == "started"
    assert result.pid is not None
    assert result.cause is None
    stray.append(result.pid)
    # The adapter returned while the leader still runs: it holds no handle and waits for nothing.
    assert alive(result.pid)
    wait_for(marker.exists, "the leader's marker file")
    wait_for(
        lambda: "leader up" in log_text(tmp_path / "out"),
        "the leader's output in the log",
    )
    assert alive(result.pid)


def test_working_directory_applied(
    tmp_path: Path, adapter: AdapterPosixProcess, stray: list[int]
) -> None:
    work = tmp_path / "work"
    work.mkdir()
    script = write_script(
        tmp_path, "cwd.py", "import os; print('cwd=' + os.getcwd(), flush=True)"
    )
    result = adapter.start_detached(python_spec(script, work))
    assert result.pid is not None
    stray.append(result.pid)
    wait_for(
        lambda: f"cwd={work.resolve()}" in log_text(tmp_path / "out"),
        "the working directory in the log",
    )


def test_environment_applied_and_nothing_inherited(
    tmp_path: Path,
    adapter: AdapterPosixProcess,
    stray: list[int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LANE_PROCESS_LEAK", "leaked")
    script = write_script(
        tmp_path,
        "env.py",
        """
        import os
        print("given=" + str(os.environ.get("LANE_PROCESS_GIVEN")), flush=True)
        print("leak=" + str(os.environ.get("LANE_PROCESS_LEAK")), flush=True)
        """,
    )
    result = adapter.start_detached(
        python_spec(script, tmp_path, environment=(("LANE_PROCESS_GIVEN", "yes"),))
    )
    assert result.pid is not None
    stray.append(result.pid)
    wait_for(
        lambda: "leak=" in log_text(tmp_path / "out"), "the environment in the log"
    )
    text = log_text(tmp_path / "out")
    assert "given=yes" in text
    assert "leak=None" in text


def test_runner_in_own_session(
    tmp_path: Path, adapter: AdapterPosixProcess, stray: list[int]
) -> None:
    script = write_script(tmp_path, "sleep.py", LONG_SLEEP)
    result = adapter.start_detached(python_spec(script, tmp_path))
    assert result.pid is not None
    stray.append(result.pid)
    assert os.getsid(result.pid) == result.pid
    assert os.getsid(result.pid) != os.getsid(0)
    assert os.getpgid(result.pid) == result.pid


# ------------------------------------------------------------------ AC2: failures typed


@pytest.mark.parametrize(
    ("kind", "cause_has"),
    [
        ("executable-absolute", "executable missing"),
        ("executable-on-path", "executable missing"),
        ("working-directory", "working directory missing"),
    ],
)
def test_start_failures_typed(tmp_path: Path, kind: str, cause_has: str) -> None:
    out = tmp_path / "out"
    adapter = AdapterPosixProcess(str(out))
    work = tmp_path
    if kind == "executable-absolute":
        spec = spec_for((str(tmp_path / "no-such-exe"),), work)
    elif kind == "executable-on-path":
        spec = spec_for(
            ("no-such-exe-on-path",), work, environment=(("PATH", str(tmp_path)),)
        )
    else:
        script = write_script(tmp_path, "ok.py", "print('x')")
        spec = python_spec(script, tmp_path / "absent")
    started = adapter.start_detached(spec)
    assert started.state == "failed"
    assert started.pid is None
    assert started.cause is not None
    assert cause_has in started.cause
    # Nothing was started: the log of the lane was never created.
    assert not (out / f"{LANE}.log").exists()


def test_a_lane_that_is_not_a_file_name_starts_nothing(tmp_path: Path) -> None:
    adapter = AdapterPosixProcess(str(tmp_path / "out"))
    result = adapter.start_detached(
        python_spec(write_script(tmp_path, "s.py", "pass"), tmp_path, lane="../escape")
    )
    assert result.state == "failed"
    assert result.cause is not None
    assert "lane is not a file name" in result.cause
    assert not (tmp_path / "escape.log").exists()


# ------------------------------------------------------------------ AC3: the script runner is refused


@pytest.mark.parametrize(
    "operation",
    [EnumLaneProcessOperation.START_DETACHED, EnumLaneProcessOperation.RUN_BOUNDED],
)
def test_refuses_script_runner(
    tmp_path: Path, operation: EnumLaneProcessOperation
) -> None:
    scripted = AdapterScriptedRunner(
        starts=(ModelLaneProcessStartResult(state="started", pid=7),),
        runs=(ModelLaneProcessRunResult(state="exited", exit_code=0),),
    )
    handler = handler_with(scripted)
    timeout = 5.0 if operation is EnumLaneProcessOperation.RUN_BOUNDED else None
    for argv in (
        (
            "python3",
            "plugins/omni/skills/remote-lane/scripts/onex_remote_lane.py",
            "run",
        ),
        ("ssh", "host", "onex_remote_lane"),
    ):
        result = handler.handle(
            ModelLaneProcessRequest(
                operation=operation,
                spec=spec_for(argv, tmp_path),
                timeout_seconds=timeout,
            )
        )
        assert result.outcome is EnumLaneProcessOutcome.REFUSED
        assert result.cause is not None
        assert "onex_remote_lane" in result.cause
    assert scripted.calls == []


# ------------------------------------------------------------------ AC3b: run_bounded


def test_run_bounded_exit_returns_the_code_and_the_standard_output(
    tmp_path: Path, adapter: AdapterPosixProcess
) -> None:
    script = write_script(
        tmp_path,
        "exit3.py",
        """
        import sys
        print("hello out")
        print("to stderr", file=sys.stderr)
        sys.exit(3)
        """,
    )
    result = adapter.run_bounded(python_spec(script, tmp_path), 20.0)
    assert result.state == "exited"
    assert result.exit_code == 3
    assert result.stdout == "hello out\n"
    assert result.cause is None
    # The child's standard error goes to the lane's log, which the adapter owns.
    assert "to stderr" in log_text(tmp_path / "out")


def test_run_bounded_exit_zero(tmp_path: Path, adapter: AdapterPosixProcess) -> None:
    script = write_script(tmp_path, "ok.py", "print('fine')")
    result = adapter.run_bounded(python_spec(script, tmp_path), 20.0)
    assert (result.state, result.exit_code, result.stdout) == ("exited", 0, "fine\n")


def test_run_bounded_timeout_kills_child(
    tmp_path: Path, adapter: AdapterPosixProcess
) -> None:
    grandchild_file = tmp_path / "grandchild.pid"
    script = write_script(
        tmp_path,
        "forker.py",
        f"""
        import pathlib, subprocess, sys, time
        child = subprocess.Popen([sys.executable, "-c", {LONG_SLEEP!r}])
        pathlib.Path({str(grandchild_file)!r}).write_text(str(child.pid))
        time.sleep(60)
        """,
    )
    started = time.monotonic()
    result = adapter.run_bounded(python_spec(script, tmp_path), 1.5)
    elapsed = time.monotonic() - started
    assert result.state == "timed_out"
    assert result.exit_code is None
    assert result.stdout == ""
    assert result.cause is None
    # The deadline ends the child: a call that waits out the child's own 60 s sleep ended nothing.
    assert elapsed < 20, (
        f"run_bounded returned after {elapsed:.1f}s for a 1.5s deadline"
    )
    wait_for(grandchild_file.exists, "the grandchild's pid file")
    grandchild = int(grandchild_file.read_text())
    wait_for(lambda: not alive(grandchild), "the grandchild to end")


@pytest.mark.parametrize("kind", ["executable", "working-directory", "timeout"])
def test_run_bounded_failure_typed(
    tmp_path: Path, adapter: AdapterPosixProcess, kind: str
) -> None:
    script = write_script(tmp_path, "ok.py", "print('x')")
    timeout = 5.0
    if kind == "executable":
        spec = spec_for((str(tmp_path / "no-such-exe"),), tmp_path)
    elif kind == "working-directory":
        spec = python_spec(script, tmp_path / "absent")
    else:
        spec, timeout = python_spec(script, tmp_path), 0.0
    result = adapter.run_bounded(spec, timeout)
    assert result.state == "failed"
    assert result.exit_code is None
    assert result.stdout == ""
    assert result.cause


# ------------------------------------------------------------------ AC4: the protocol and both adapters


class Harness(Protocol):
    runner: ProtocolLaneProcessRunner

    def good_start(self) -> ModelLaneStartSpec: ...
    def bad_start(self) -> ModelLaneStartSpec: ...
    def exits(self, code: int, out: str) -> ModelLaneStartSpec: ...
    def hangs(self) -> ModelLaneStartSpec: ...


class PosixHarness:
    def __init__(self, directory: Path, stray: list[int]) -> None:
        self.directory = directory
        self.stray = stray
        self.runner: ProtocolLaneProcessRunner = AdapterPosixProcess(
            str(directory / "out")
        )

    def good_start(self) -> ModelLaneStartSpec:
        return python_spec(
            write_script(self.directory, "g.py", LONG_SLEEP), self.directory
        )

    def bad_start(self) -> ModelLaneStartSpec:
        return spec_for((str(self.directory / "no-such-exe"),), self.directory)

    def exits(self, code: int, out: str) -> ModelLaneStartSpec:
        script = write_script(
            self.directory,
            "e.py",
            f"import sys; print({out!r}, end=''); sys.exit({code})",
        )
        return python_spec(script, self.directory)

    def hangs(self) -> ModelLaneStartSpec:
        return python_spec(
            write_script(self.directory, "h.py", LONG_SLEEP), self.directory
        )


class ScriptedHarness:
    """The scripted adapter answers what each spec's operation was scripted to answer, in call order."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.scripted = AdapterScriptedRunner(
            starts=(
                ModelLaneProcessStartResult(state="started", pid=4242),
                ModelLaneProcessStartResult(
                    state="failed", cause="executable missing: x"
                ),
            ),
            runs=(
                ModelLaneProcessRunResult(state="exited", exit_code=3, stdout="out"),
                ModelLaneProcessRunResult(state="timed_out"),
                ModelLaneProcessRunResult(
                    state="failed", cause="executable missing: x"
                ),
            ),
        )
        self.runner: ProtocolLaneProcessRunner = self.scripted

    def _spec(self) -> ModelLaneStartSpec:
        return spec_for(("scripted",), self.directory)

    good_start = bad_start = hangs = _spec

    def exits(self, code: int, out: str) -> ModelLaneStartSpec:
        return self._spec()


def conformance_checks(h: Any, *, scripted: bool) -> None:
    """What every adapter of ProtocolLaneProcessRunner answers, whatever it runs."""
    assert isinstance(h.runner, ProtocolLaneProcessRunner)
    started = h.runner.start_detached(h.good_start())
    assert started.state == "started"
    assert started.pid is not None
    assert started.pid >= 1
    assert started.cause is None
    if not scripted:
        h.stray.append(started.pid)
    failed = h.runner.start_detached(h.bad_start())
    assert failed.state == "failed"
    assert failed.pid is None
    assert failed.cause
    exited = h.runner.run_bounded(h.exits(3, "out"), 20.0)
    assert exited.state == "exited"
    assert exited.exit_code == 3
    assert exited.stdout == "out"
    timed_out = h.runner.run_bounded(h.hangs(), 1.0)
    assert timed_out.state == "timed_out"
    assert timed_out.exit_code is None
    assert timed_out.stdout == ""
    assert timed_out.cause is None
    unrunnable = h.runner.run_bounded(h.bad_start(), 5.0)
    assert unrunnable.state == "failed"
    assert unrunnable.cause


def test_protocol_conformance_posix_adapter(tmp_path: Path, stray: list[int]) -> None:
    conformance_checks(PosixHarness(tmp_path, stray), scripted=False)


def test_protocol_conformance_scripted_adapter(tmp_path: Path) -> None:
    conformance_checks(ScriptedHarness(tmp_path), scripted=True)


def test_protocol_conformance_refuses_an_adapter_that_reports_a_timeout_as_an_exit(
    tmp_path: Path,
) -> None:
    class Broken(ScriptedHarness):
        def __init__(self, directory: Path) -> None:
            super().__init__(directory)
            self.scripted = AdapterScriptedRunner(
                starts=(
                    ModelLaneProcessStartResult(state="started", pid=4242),
                    ModelLaneProcessStartResult(state="failed", cause="c"),
                ),
                runs=(
                    ModelLaneProcessRunResult(
                        state="exited", exit_code=3, stdout="out"
                    ),
                    ModelLaneProcessRunResult(state="exited", exit_code=0),
                    ModelLaneProcessRunResult(state="failed", cause="c"),
                ),
            )
            self.runner = self.scripted

    with pytest.raises(AssertionError):
        conformance_checks(Broken(tmp_path), scripted=True)


# ------------------------------------------------------------------ the handler maps each outcome


def test_handler_maps_each_operation_and_outcome(tmp_path: Path) -> None:
    scripted = AdapterScriptedRunner(
        starts=(
            ModelLaneProcessStartResult(state="started", pid=9),
            ModelLaneProcessStartResult(
                state="failed", cause="working directory missing: /x"
            ),
        ),
        runs=(
            ModelLaneProcessRunResult(state="exited", exit_code=0, stdout="ok"),
            ModelLaneProcessRunResult(state="timed_out"),
            ModelLaneProcessRunResult(state="failed", cause="executable missing: y"),
        ),
    )
    handler = handler_with(scripted)
    spec = spec_for(("cmd",), tmp_path)

    def start() -> Any:
        return handler.handle(
            ModelLaneProcessRequest(
                operation=EnumLaneProcessOperation.START_DETACHED, spec=spec
            )
        )

    def run(seconds: float) -> Any:
        return handler.handle(
            ModelLaneProcessRequest(
                operation=EnumLaneProcessOperation.RUN_BOUNDED,
                spec=spec,
                timeout_seconds=seconds,
            )
        )

    started = start()
    assert (started.outcome, started.pid, started.cause) == (
        EnumLaneProcessOutcome.STARTED,
        9,
        None,
    )
    failed = start()
    assert failed.outcome is EnumLaneProcessOutcome.FAILED
    assert failed.pid is None
    assert failed.cause == "working directory missing: /x"
    exited = run(7.5)
    assert (exited.outcome, exited.exit_code, exited.stdout) == (
        EnumLaneProcessOutcome.EXITED,
        0,
        "ok",
    )
    assert run(7.5).outcome is EnumLaneProcessOutcome.TIMED_OUT
    assert run(7.5).cause == "executable missing: y"
    assert [name for name, _, _ in scripted.calls] == [
        "start_detached",
        "start_detached",
        "run_bounded",
        "run_bounded",
        "run_bounded",
    ]
    assert scripted.calls[2][2] == 7.5


def test_request_deadline_follows_the_operation(tmp_path: Path) -> None:
    spec = spec_for(("cmd",), tmp_path)
    with pytest.raises(ValidationError, match="run_bounded requires timeout_seconds"):
        ModelLaneProcessRequest(
            operation=EnumLaneProcessOperation.RUN_BOUNDED, spec=spec
        )
    with pytest.raises(ValidationError, match="carries no timeout_seconds"):
        ModelLaneProcessRequest(
            operation=EnumLaneProcessOperation.START_DETACHED,
            spec=spec,
            timeout_seconds=1.0,
        )


# ------------------------------------------------------------------ G-NODE: contract, overlay keys, seams


def contract() -> dict[str, Any]:
    loaded = yaml.safe_load((NODE_DIR / "contract.yaml").read_text())
    assert isinstance(loaded, dict)
    return loaded


def test_contract_loads_with_archetype_models_routing_and_descriptor() -> None:
    loaded = contract()
    assert loaded["name"] == "node_lane_process_effect"
    assert loaded["node_type"] == "effect"
    assert loaded["descriptor"]["node_archetype"] == "effect"
    assert {r["operation"] for r in loaded["handler_routing"]["handlers"]} == {
        "start_detached",
        "run_bounded",
    }
    for route in loaded["handler_routing"]["handlers"]:
        for dotted in (route["input_model"], route["output_model"]):
            module, _, name = dotted.rpartition(".")
            assert getattr(importlib.import_module(module), name)
        module = importlib.import_module(route["handler"]["module"])
        assert getattr(module, route["handler"]["name"]) is HandlerLaneProcess
    metadata = yaml.safe_load((NODE_DIR / "metadata.yaml").read_text())
    assert metadata["name"] == loaded["name"]


def test_contract_declares_the_overlay_keys_the_node_reads() -> None:
    assert contract()["deployment_overlay"]["fields"] == ["floor_fill.output_dir"]


def test_seams_in_spi_each_injected_protocol_resolves_into_omnibase_spi() -> None:
    listed = []
    for dep in contract()["protocol_dependencies"]:
        module, _, name = dep["protocol"].partition(":")
        resolved = getattr(importlib.import_module(module), name)
        listed.append((dep["name"], resolved.__module__))
    assert listed == [
        ("runner", "omnibase_spi.protocols.lane.protocol_lane_process_runner")
    ]
    assert all(module.startswith("omnibase_spi") for _, module in listed)
    # The handler asks its container for exactly the declared protocol, and only when an operation runs.
    runner = AdapterScriptedRunner(
        starts=(ModelLaneProcessStartResult(state="started", pid=5),)
    )
    container = FakeContainer(runner)
    handler = HandlerLaneProcess(cast("ModelONEXContainer", container))
    assert container.asked == []
    handler.handle(
        ModelLaneProcessRequest(
            operation=EnumLaneProcessOperation.START_DETACHED,
            spec=spec_for(("cmd",), Path.cwd()),
        )
    )
    assert container.asked == [ProtocolLaneProcessRunner]


def test_handler_does_no_io_and_reads_no_environment() -> None:
    source = (NODE_DIR / "handlers" / "handler_lane_process.py").read_text()
    for forbidden in (
        "subprocess",
        "os.environ",
        "open(",
        "Path(",
        "datetime",
        "import os",
    ):
        assert forbidden not in source, forbidden


def test_config_variation_output_dir_changes_where_the_log_is_written(
    tmp_path: Path,
) -> None:
    script = write_script(tmp_path, "hello.py", "print('hello log')")
    for name in ("first", "second"):
        out = tmp_path / name
        overlay = ModelLaneProcessOverlay.model_validate(
            {"floor_fill": {"output_dir": str(out)}}
        )
        adapter = AdapterPosixProcess.from_overlay(overlay)
        result = adapter.run_bounded(
            python_spec(script, tmp_path, lane=f"lane-{name}"), 20.0
        )
        assert result.state == "exited"
        assert (out / f"lane-{name}.log").exists()
    assert not (tmp_path / "first" / "lane-second.log").exists()


@pytest.mark.parametrize(
    ("overlay", "names"),
    [
        ({}, "floor_fill"),
        ({"floor_fill": {}}, "floor_fill.output_dir"),
        ({"floor_fill": {"output_dir": ""}}, "floor_fill.output_dir"),
    ],
)
def test_missing_key_refused_naming_the_overlay_key(
    overlay: dict[str, Any], names: str
) -> None:
    with pytest.raises(ValidationError, match=re.escape(names)):
        ModelLaneProcessOverlay.model_validate(overlay)


def test_missing_key_refused_by_the_adapter_naming_the_overlay_key() -> None:
    with pytest.raises(ValueError, match=r"floor_fill\.output_dir"):
        AdapterPosixProcess("")


def test_the_example_overlay_loads_and_names_no_script_runner() -> None:
    text = (NODE_DIR / "overlay.example.yaml").read_text()
    overlay = ModelLaneProcessOverlay.model_validate(yaml.safe_load(text))
    assert overlay.floor_fill.output_dir
    assert "onex_remote_lane" not in text


def test_node_holds_no_ticket_id_in_its_python_files() -> None:
    for path in NODE_DIR.rglob("*.py"):
        assert not re.search(r"OMN-\d+", path.read_text()), path


def test_sys_executable_is_the_interpreter_the_tests_start() -> None:
    assert os.path.isabs(sys.executable)
