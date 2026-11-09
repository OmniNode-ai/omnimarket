# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The installer gives the silent-failure alarm a dispatch command when it is given the runner's path.

The alarm's configuration was installed with no ``dispatch_command``, so on 2026-11-09 a job that exited 1 five
times in a row (the lab broker-lag alarm) opened an episode and no fix lane was sent. These tests render the
declared document with and without the ``remote_lane_runner`` input and run the alarm's own handler on the result.
"""

from __future__ import annotations

import copy
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units import (
    HandlerRegistryUnits,
    load_units,
)
from omnimarket.nodes.node_host_installer_registry_units.models.model_registry_units import (
    ModelRegistryUnitsRequest,
)
from omnimarket.nodes.node_silent_failure_alarm_effect.handlers.handler_silent_failure_alarm import (
    HandlerSilentFailureAlarm,
)
from omnimarket.nodes.node_silent_failure_alarm_effect.models import (
    ModelSilentFailureAlarmOptions,
    ModelSilentFailureAlarmRequest,
)
from tests.nodes.node_silent_failure_alarm_compute.test_golden_chain_silent_failure_alarm_compute import (
    HOST_NOW,
    world,
)
from tests.nodes.node_silent_failure_alarm_effect.test_job_exit_row_matches_dispatch_omn22881 import (
    _launchctl,
)

pytestmark = pytest.mark.unit

UNIT = "silent-failure-alarm"
REVISION = "c" * 40


@pytest.fixture(autouse=True)
def _host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units.interpreter_present",
        lambda _path: True,
    )
    for name in (
        "ONEX_RUNTIME_SSH_HOST",
        "ONEX_ALARM_APPENDER_ROOT",
        "ONEX_ALARM_HOST_TABLE",
        "ONEX_ALARM_REMOTE_LANE_RUNNER",
    ):
        monkeypatch.delenv(name, raising=False)


def _run(argv: list[str]) -> Any:
    if argv[-2:] == ["rev-parse", "HEAD"]:
        return subprocess.CompletedProcess(argv, 0, REVISION + "\n", "")
    if argv[-1] == "-u":
        return subprocess.CompletedProcess(argv, 0, "501\n", "")
    if argv[-1] == "-un":
        return subprocess.CompletedProcess(argv, 0, "fixture-user\n", "")
    return subprocess.CompletedProcess(argv, 0, "", "")


def _request(tmp_path: Path, **kw: Any) -> ModelRegistryUnitsRequest:
    fields: dict[str, Any] = {
        "unit": UNIT,
        "home": str(tmp_path / "home"),
        "registry_root": str(tmp_path / "omni"),
        "workspace_config_root": str(tmp_path / "ws"),
        "runtime_ssh_host": "lab-user@runtime.example.test",
        "appender_root": str(tmp_path / "appender"),
        "host_table_path": str(tmp_path / "hosts.yaml"),
    }
    fields.update(kw)
    return ModelRegistryUnitsRequest(**fields)


def _handler() -> HandlerRegistryUnits:
    return HandlerRegistryUnits(run=_run, units=load_units())


def test_the_runner_path_becomes_the_alarms_dispatch_command(tmp_path: Path) -> None:
    runner = tmp_path / "plugin/skills/remote-lane/scripts/onex_remote_lane.py"
    result = _handler().handle(_request(tmp_path, remote_lane_runner=str(runner)))
    assert result.exit_code == 0, result.stderr
    document = json.loads(result.config)
    assert document["dispatch_command"] == [
        str(tmp_path / "appender/.venv/bin/python"),
        str(runner),
        "run",
    ]
    options = ModelSilentFailureAlarmOptions.model_validate(document)
    assert options.dispatch_command == tuple(document["dispatch_command"])


def test_control_without_the_runner_path_the_document_has_no_dispatch_command(
    tmp_path: Path,
) -> None:
    result = _handler().handle(_request(tmp_path))
    assert result.exit_code == 0, result.stderr
    document = json.loads(result.config)
    assert "dispatch_command" not in document
    assert (
        ModelSilentFailureAlarmOptions.model_validate(document).dispatch_command is None
    )


def test_the_shell_then_the_last_install_supply_the_runner_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = tmp_path / "plugin/onex_remote_lane.py"
    monkeypatch.setenv("ONEX_ALARM_REMOTE_LANE_RUNNER", str(runner))
    installed = _handler().handle(_request(tmp_path, mode="install"))
    assert installed.exit_code == 0, installed.stderr
    monkeypatch.delenv("ONEX_ALARM_REMOTE_LANE_RUNNER")
    again = _handler().handle(_request(tmp_path, mode="install"))
    assert again.exit_code == 0, again.stderr
    assert json.loads(again.config)["dispatch_command"][1] == str(runner)


def test_a_key_that_is_not_declared_optional_still_refuses_a_hole(
    tmp_path: Path,
) -> None:
    units = copy.deepcopy(load_units())
    units[UNIT]["config_file"]["optional_keys"] = []
    result = HandlerRegistryUnits(run=_run, units=units).handle(_request(tmp_path))
    assert result.exit_code == 1
    assert "{remote_lane_runner}" in result.stderr


def _alarm_rows(tmp_path: Path, command: tuple[str, ...] | None) -> tuple[str, ...]:
    options: ModelSilentFailureAlarmOptions = world(tmp_path).model_copy(
        update={
            "launchctl": str(_launchctl(tmp_path)),
            "dispatch_command": command,
            "brief_dir": tmp_path / "briefs",
        }
    )
    result = HandlerSilentFailureAlarm(options).handle(
        ModelSilentFailureAlarmRequest(now=HOST_NOW, apply=True)
    )
    return tuple(result.rows) + tuple(result.warnings)


def test_an_unset_command_is_named_by_its_real_setting(tmp_path: Path) -> None:
    text = "\n".join(_alarm_rows(tmp_path, None))
    assert "dispatch_command unset in the alarm configuration" in text
    assert "ONEX_ALARM_DISPATCH_COMMAND" not in text


def test_a_command_naming_an_absent_runner_is_not_reported_dispatched(
    tmp_path: Path,
) -> None:
    text = "\n".join(
        _alarm_rows(
            tmp_path, (sys.executable, str(tmp_path / "absent-runner.py"), "run")
        )
    )
    assert "dispatch command names a path that is absent" in text
    assert "dispatched=no" in text


def _recorder(tmp_path: Path) -> tuple[Path, Path]:
    """A stand-in remote-lane runner: it appends one line per call to a file, so a run counts the requests sent."""
    calls = tmp_path / "calls.log"
    script = tmp_path / "recorder.py"
    script.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        f"open({str(calls)!r}, 'a', encoding='utf-8').write(' '.join(sys.argv[1:]) + '\\n')\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script, calls


def _calls(calls: Path) -> list[str]:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if calls.exists() and calls.read_text(encoding="utf-8").strip():
            time.sleep(0.5)
            return calls.read_text(encoding="utf-8").splitlines()
        time.sleep(0.1)
    return []


def test_an_exit_1_streak_with_a_dispatch_command_sends_exactly_one_fix_lane_request(
    tmp_path: Path,
) -> None:
    """The declared unit renders the command, and the alarm built from that document sends one request."""
    runner, calls = _recorder(tmp_path)
    interpreter = tmp_path / "appender/.venv/bin/python"
    interpreter.parent.mkdir(parents=True)
    interpreter.symlink_to(sys.executable)
    rendered = _handler().handle(_request(tmp_path, remote_lane_runner=str(runner)))
    assert rendered.exit_code == 0, rendered.stderr
    command = ModelSilentFailureAlarmOptions.model_validate(
        json.loads(rendered.config)
    ).dispatch_command
    assert command == (str(interpreter), str(runner), "run")
    rows = _alarm_rows(tmp_path, command)
    assert any("lane job-exit-fix dispatched for episode" in row for row in rows), rows
    sent = _calls(calls)
    assert len(sent) == 1, sent
    assert sent[0].startswith("run --brief ")
    assert "--lane job-exit-fix-" in sent[0]


def test_control_an_exit_1_streak_with_no_dispatch_command_sends_no_fix_lane_request(
    tmp_path: Path,
) -> None:
    _runner, calls = _recorder(tmp_path)
    rows = _alarm_rows(tmp_path, None)
    assert not any("dispatched for episode" in row for row in rows), rows
    assert _calls(calls) == []
