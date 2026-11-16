# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The three landing runtime mains answer a bare-shell probe without a pass (OMN-23604).

The installer's start check must prove that a unit starts from a bare environment, so each main
answers ``--probe`` with one JSON line, ``{"status": "probe_ok"}``, and exits 0 without loading
an overlay, reading the bus or writing a file; ``--help`` exits 0 the same way; an unknown
argument is refused before any pass; no argument keeps today's behaviour. End to end: the
subprocess run from a bare shell writes nothing under its state directory.
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
def test_golden_probe_does_no_pass(
    node_module: str,
    runtime_name: str,
    loader_name: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runtime = importlib.import_module(runtime_name)
    loader_calls: list[object] = []
    run_calls: list[object] = []
    monkeypatch.setattr(
        runtime, loader_name, _refusing(loader_calls, "the probe loaded the overlay")
    )
    monkeypatch.setattr(
        runtime, "run_once", _refusing(run_calls, "the probe ran the pass")
    )
    assert runtime.main(["--probe"]) == 0
    out = capsys.readouterr().out
    lines = out.splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0]) == {"status": "probe_ok"}
    assert loader_calls == []
    assert run_calls == []


@pytest.mark.parametrize(
    ("node_module", "runtime_name", "loader_name"),
    UNITS,
    ids=UNITS_IDS,
)
def test_golden_help_does_no_pass(
    node_module: str,
    runtime_name: str,
    loader_name: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runtime = importlib.import_module(runtime_name)
    loader_calls: list[object] = []
    run_calls: list[object] = []
    monkeypatch.setattr(
        runtime, loader_name, _refusing(loader_calls, "the help loaded the overlay")
    )
    monkeypatch.setattr(
        runtime, "run_once", _refusing(run_calls, "the help ran the pass")
    )
    with pytest.raises(SystemExit) as exc:
        runtime.main(["--help"])
    assert exc.value.code == 0
    assert "--probe" in capsys.readouterr().out
    assert loader_calls == []
    assert run_calls == []


@pytest.mark.parametrize(
    ("node_module", "runtime_name", "loader_name"),
    UNITS,
    ids=UNITS_IDS,
)
def test_error_unknown_argument_is_refused_before_any_pass(
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
        runtime.main(["--nope"])
    assert exc.value.code == 2
    assert loader_calls == []
    assert run_calls == []


@pytest.mark.parametrize(
    ("node_module", "runtime_name", "loader_name"),
    UNITS,
    ids=UNITS_IDS,
)
def test_control_no_argument_still_runs_the_pass(
    node_module: str,
    runtime_name: str,
    loader_name: str,
    monkeypatch: pytest.MonkeyPatch,
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
    assert runtime.main([]) == 0
    assert len(run_calls) == 1


@pytest.mark.parametrize(
    ("node_module", "_runtime_name", "_loader_name"),
    UNITS,
    ids=UNITS_IDS,
)
def test_probe_from_a_bare_shell_exits_zero_and_writes_nothing(
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
        [*base, "--probe"],
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
    help_run = subprocess.run(
        [*base, "--help"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert help_run.returncode == 0, help_run.stderr
    assert list(tmp_path.rglob("*")) == []
