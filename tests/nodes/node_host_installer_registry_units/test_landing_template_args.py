# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The three landing runtime mains accept the registry tick template's arguments (OMN-23662).

Each main accepts ``--omni-home`` and ``--state-dir`` (both optional) and, after a pass, writes
``last-run.json`` into the state directory recording the exit code, the result line and a
``ran_at`` timestamp. The probe writes nothing. A pass that fails still writes the heartbeat.
An overlay refusal is recorded as exit 3 with ``overlay_refused``. No ``--state-dir`` means no
file is written. An unknown argument is refused before any pass. End to end: the subprocess run
from a bare shell with the template's arguments writes nothing under its state directory.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from omnimarket.handlers.node_overlay_reader import NodeOverlayError

UNITS = (
    (
        "omnimarket.nodes.node_pr_landing_snapshot_effect",
        "omnimarket.nodes.node_pr_landing_snapshot_effect.runtime_snapshot",
        "load_structured_overlay",
    ),
    (
        "omnimarket.nodes.node_pr_landing_watcher_facts_effect",
        "omnimarket.nodes.node_pr_landing_watcher_facts_effect.runtime_watcher_facts",
        "load_structured_overlay",
    ),
    (
        "omnimarket.nodes.node_pr_landing_tick_orchestrator",
        "omnimarket.nodes.node_pr_landing_tick_orchestrator.runtime_tick",
        "load_tick_overlay",
    ),
)

UNITS_IDS = (
    "node_pr_landing_snapshot_effect",
    "node_pr_landing_watcher_facts_effect",
    "node_pr_landing_tick_orchestrator",
)

pytestmark = pytest.mark.unit


def _refusing(calls: list[object], reason: str) -> Any:
    def refuse(*_args: object, **_kwargs: object) -> Any:
        calls.append(None)
        pytest.fail(reason)

    return refuse


@pytest.mark.parametrize(
    ("node_module", "runtime_name", "loader_name"),
    UNITS,
    ids=UNITS_IDS,
)
def test_golden_pass_writes_heartbeat(
    node_module: str,
    runtime_name: str,
    loader_name: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime = importlib.import_module(runtime_name)
    state = tmp_path / "state"
    sentinel = object()
    run_calls: list[object] = []

    def fake_loader(*_args: object, **_kwargs: object) -> Any:
        return sentinel

    def fake_run(*_args: object, **_kwargs: object) -> tuple[int, dict[str, str]]:
        run_calls.append(None)
        return 0, {"status": "ran"}

    monkeypatch.setattr(runtime, loader_name, fake_loader)
    monkeypatch.setattr(runtime, "run_once", fake_run)
    assert runtime.main(["--omni-home", str(tmp_path), "--state-dir", str(state)]) == 0
    assert len(run_calls) == 1
    target = state / "last-run.json"
    assert target.is_file()
    record = json.loads(target.read_text(encoding="utf-8"))
    assert record["exit_code"] == 0
    assert record["result"] == {"status": "ran"}
    assert isinstance(record["ran_at"], str)
    assert record["ran_at"]


@pytest.mark.parametrize(
    ("node_module", "runtime_name", "loader_name"),
    UNITS,
    ids=UNITS_IDS,
)
def test_golden_probe_writes_nothing(
    node_module: str,
    runtime_name: str,
    loader_name: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    runtime = importlib.import_module(runtime_name)
    state = tmp_path / "state"
    loader_calls: list[object] = []
    run_calls: list[object] = []
    monkeypatch.setattr(
        runtime, loader_name, _refusing(loader_calls, "the probe loaded the overlay")
    )
    monkeypatch.setattr(
        runtime, "run_once", _refusing(run_calls, "the probe ran the pass")
    )
    assert (
        runtime.main(
            ["--omni-home", str(tmp_path), "--state-dir", str(state), "--probe"]
        )
        == 0
    )
    out = capsys.readouterr().out
    lines = out.splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0]) == {"status": "probe_ok"}
    assert loader_calls == []
    assert run_calls == []
    assert not state.exists()


@pytest.mark.parametrize(
    ("node_module", "runtime_name", "loader_name"),
    UNITS,
    ids=UNITS_IDS,
)
def test_error_failed_pass_writes_heartbeat(
    node_module: str,
    runtime_name: str,
    loader_name: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime = importlib.import_module(runtime_name)
    state = tmp_path / "state"
    sentinel = object()
    run_calls: list[object] = []

    def fake_loader(*_args: object, **_kwargs: object) -> Any:
        return sentinel

    def fake_run(*_args: object, **_kwargs: object) -> tuple[int, dict[str, str]]:
        run_calls.append(None)
        return 1, {"status": "failed"}

    monkeypatch.setattr(runtime, loader_name, fake_loader)
    monkeypatch.setattr(runtime, "run_once", fake_run)
    assert runtime.main(["--omni-home", str(tmp_path), "--state-dir", str(state)]) == 1
    assert len(run_calls) == 1
    target = state / "last-run.json"
    assert target.is_file()
    record = json.loads(target.read_text(encoding="utf-8"))
    assert record["exit_code"] == 1
    assert record["result"] == {"status": "failed"}


@pytest.mark.parametrize(
    ("node_module", "runtime_name", "loader_name"),
    UNITS,
    ids=UNITS_IDS,
)
def test_error_overlay_refusal_writes_heartbeat(
    node_module: str,
    runtime_name: str,
    loader_name: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime = importlib.import_module(runtime_name)
    state = tmp_path / "state"
    if loader_name == "load_tick_overlay":

        def raising_loader(*_args: object, **_kwargs: object) -> Any:
            raise ValueError("x")
    else:

        def raising_loader(*_args: object, **_kwargs: object) -> Any:
            raise NodeOverlayError("x")

    monkeypatch.setattr(runtime, loader_name, raising_loader)
    assert runtime.main(["--omni-home", str(tmp_path), "--state-dir", str(state)]) == 3
    target = state / "last-run.json"
    assert target.is_file()
    record = json.loads(target.read_text(encoding="utf-8"))
    assert record["exit_code"] == 3
    assert record["result"]["status"] == "overlay_refused"


@pytest.mark.parametrize(
    ("node_module", "runtime_name", "loader_name"),
    UNITS,
    ids=UNITS_IDS,
)
def test_control_no_state_dir_writes_nothing(
    node_module: str,
    runtime_name: str,
    loader_name: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime = importlib.import_module(runtime_name)
    sentinel = object()
    run_calls: list[object] = []

    def fake_loader(*_args: object, **_kwargs: object) -> Any:
        return sentinel

    def fake_run(*_args: object, **_kwargs: object) -> tuple[int, dict[str, str]]:
        run_calls.append(None)
        return 0, {"status": "ran"}

    monkeypatch.setattr(runtime, loader_name, fake_loader)
    monkeypatch.setattr(runtime, "run_once", fake_run)
    assert runtime.main(["--omni-home", str(tmp_path)]) == 0
    assert len(run_calls) == 1
    assert list(tmp_path.rglob("*")) == []


@pytest.mark.parametrize(
    ("node_module", "runtime_name", "loader_name"),
    UNITS,
    ids=UNITS_IDS,
)
def test_control_unknown_argument_is_refused_before_any_pass(
    node_module: str,
    runtime_name: str,
    loader_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = importlib.import_module(runtime_name)
    loader_calls: list[object] = []
    run_calls: list[object] = []
    monkeypatch.setattr(
        runtime,
        loader_name,
        _refusing(loader_calls, "an unknown argument loaded the overlay"),
    )
    monkeypatch.setattr(
        runtime, "run_once", _refusing(run_calls, "an unknown argument ran the pass")
    )
    with pytest.raises(SystemExit) as exc:
        runtime.main(["--bogus"])
    assert exc.value.code == 2
    assert loader_calls == []
    assert run_calls == []


@pytest.mark.parametrize(
    ("node_module", "_runtime_name", "_loader_name"),
    UNITS,
    ids=UNITS_IDS,
)
def test_seam_bare_shell_pass_writes_nothing(
    node_module: str,
    _runtime_name: str,
    _loader_name: str,
    tmp_path: Path,
) -> None:
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path),
        "ONEX_STATE_DIR": str(tmp_path / "state"),
    }
    base = [sys.executable, "-m", node_module]
    probe = subprocess.run(
        [
            *base,
            "--omni-home",
            str(tmp_path / "root"),
            "--state-dir",
            str(tmp_path / "state"),
            "--probe",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert probe.returncode == 0, probe.stderr
    assert probe.stdout.strip() == '{"status": "probe_ok"}'
    assert list(tmp_path.rglob("*")) == []
