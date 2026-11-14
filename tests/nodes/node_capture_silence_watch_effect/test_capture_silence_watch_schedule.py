# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The capture silence watch is scheduled on every lab host.

The schedule is the host installer's ``capture-silence-watch`` systemd unit; its settings are the node's contract.
These tests read the declaration, the unit it renders, and one tick of the module the unit runs.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env

from omnimarket.nodes.node_capture_silence_watch_effect import (
    runtime_capture_silence_watch_tick as tick,
)
from omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units import (
    HandlerRegistryUnits,
    load_units,
)
from omnimarket.nodes.node_host_installer_registry_units.models.model_registry_units import (
    ModelRegistryUnitsRequest,
)

UNIT = "capture-silence-watch"
LABEL = "onex-capture-silence-watch"
NODE = "node_capture_silence_watch_effect"
ROOT = Path(__file__).resolve().parents[3]
NODE_DIR = ROOT / "src" / "omnimarket" / "nodes" / NODE
HANDLER = "omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units"
LINUX_PYTHON = "/usr/bin/python3.12"


def _git(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        env=scrub_git_location_env(os.environ),
        capture_output=True,
        text=True,
        check=True,
    )


def _checkout(path: Path, entry: str) -> str:
    path.mkdir(parents=True)
    _git(["git", "-C", str(path), "init", "-q", "-b", "branch"])
    (path / entry).parent.mkdir(parents=True)
    (path / entry).write_text("print('tick')\n", encoding="utf-8")
    _git(["git", "-C", str(path), "add", "-A"])
    _git(
        [
            *[
                "git",
                "-C",
                str(path),
                "-c",
                "user.name=t",
                "-c",
                "user.email=t@example.com",
                "commit",
                "-q",
                "-m",
                "x",
            ],
        ]
    )
    return _git(["git", "-C", str(path), "rev-parse", "HEAD"]).stdout.strip()


def _recorder(argv: list[str]) -> subprocess.CompletedProcess[str]:
    if argv[0] in ("git", "id"):
        return subprocess.run(
            argv,
            env=scrub_git_location_env(os.environ),
            capture_output=True,
            text=True,
            check=False,
        )
    return subprocess.CompletedProcess(argv, 0, "", "")


@pytest.fixture
def linux_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        f"{HANDLER}.interpreter_present", lambda path: path == LINUX_PYTHON
    )
    monkeypatch.setattr(
        "shutil.which", lambda name: "/usr/bin/uv" if name == "uv" else None
    )


def _render(tmp_path: Path, units: dict[str, Any] | None = None) -> Any:
    declared = load_units() if units is None else units
    entry = declared[UNIT]["entry_file"]
    path = tmp_path / "branch"
    revision = _checkout(path, entry)
    return HandlerRegistryUnits(_recorder, units=declared).handle(
        ModelRegistryUnitsRequest(
            unit=UNIT,
            home=str(tmp_path / "home"),
            registry_root=str(tmp_path / "omni"),
            project_path=str(path),
            expected_revision=revision,
        )
    )


# --- the schedule is declared -----------------------------------------------------------------


def test_the_schedule_is_declared_for_a_linux_host_with_a_bounded_run() -> None:
    declared = load_units()[UNIT]
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text(encoding="utf-8"))
    assert declared["platform"] == "systemd"
    assert declared["kind"] == "registry_module_tick"
    assert declared["label"] == LABEL
    assert declared["schedule"]["start_interval"] > 0
    # The unit ends a run at the bound the node declares, and before the next tick is due.
    limit = declared["schedule"]["run_timeout_s"]
    assert limit * 1000 == contract["descriptor"]["timeout_ms"]
    assert limit < declared["schedule"]["start_interval"]
    assert (ROOT / declared["entry_file"]).is_file()
    assert declared["module"] == (
        declared["entry_file"]
        .removeprefix("src/")
        .removesuffix(".py")
        .replace("/", ".")
    )


def test_the_rendered_unit_runs_the_tick_module_and_names_no_tmp_path(
    tmp_path: Path, linux_host: None
) -> None:
    result = _render(tmp_path)
    assert result.status == "success", result.stderr
    exec_line = next(
        line for line in result.rendered.splitlines() if line.startswith("ExecStart=")
    )
    assert (
        f'"-m" "omnimarket.nodes.{NODE}.runtime_capture_silence_watch_tick"'
        in exec_line
    )
    assert '"--omni-home"' in exec_line
    assert '"--state-dir"' in exec_line
    home = tmp_path / "home"
    assert (
        f"EnvironmentFile=-{home}/.config/onex/capture-silence-watch.env"
        in result.rendered
    )
    # The unit runs the tick module from the registry project, not the /tmp copy of the source it ran from before.
    assert "/tmp/capture-silence-watch" not in result.rendered
    assert f'"--project" "{tmp_path / "branch"}"' in exec_line


# --- one tick ---------------------------------------------------------------------------------


def test_a_tick_records_success_and_runs_the_node_tick_operation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Seam: the unit's tick module and the node's own command line. The node takes the operation `tick` and nothing else
    # from the unit; the unit's --omni-home and --state-dir stop in the tick module.
    seen: list[list[str]] = []

    def _node_main(argv: list[str]) -> int:
        seen.append(argv)
        return 0

    monkeypatch.setattr(tick, "node_main", _node_main)
    state = tmp_path / "state"
    code = tick.main(["--omni-home", str(tmp_path), "--state-dir", str(state)])
    assert code == 0
    evidence = json.loads((state / "last-run.json").read_text(encoding="utf-8"))
    assert evidence["exit_code"] == 0
    assert "ran_at" in evidence
    assert seen == [["tick"]]


def test_the_node_command_line_still_takes_the_tick_operation(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Seam, node side: when the node's command line stops accepting `tick`, this fails here and not on a host.
    from omnimarket.nodes.node_capture_silence_watch_effect import __main__ as node_cli

    def _stop(**_: object) -> object:
        raise ValueError("reached the handler")

    monkeypatch.setattr(node_cli, "build_handler", _stop)
    assert node_cli.main(["tick"]) == 3
    assert "INDETERMINATE: reached the handler" in capsys.readouterr().err
    # Control: an operation the node does not take is refused before the handler is built.
    assert node_cli.main(["unknown-operation"]) == 3
    assert "unknown argument" in capsys.readouterr().err


def test_a_tick_that_raises_records_indeterminate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _raise(argv: list[str]) -> int:
        raise ValueError("boom")

    monkeypatch.setattr(tick, "node_main", _raise)
    state = tmp_path / "state"
    code = tick.main(["--omni-home", str(tmp_path), "--state-dir", str(state)])
    assert code == 3
    evidence = json.loads((state / "last-run.json").read_text(encoding="utf-8"))
    assert evidence["exit_code"] == 3
    assert evidence["indeterminate"] == "boom"


def test_a_tick_that_returns_two_is_passed_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tick, "node_main", lambda _argv: 2)
    state = tmp_path / "state"
    code = tick.main(["--omni-home", str(tmp_path), "--state-dir", str(state)])
    assert code == 2
    evidence = json.loads((state / "last-run.json").read_text(encoding="utf-8"))
    assert evidence["exit_code"] == 2


def test_tick_requires_omni_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tick, "node_main", lambda _argv: 0)
    with pytest.raises(SystemExit):
        tick.main(["--state-dir", str(tmp_path)])
