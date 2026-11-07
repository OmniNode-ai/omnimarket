# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The installer writes the silent-failure alarm's configuration from the unit declaration (OMN-22778).

On 2026-11-06 the alarm unit's configuration file was written by hand from the old unit's plist, so a
reinstall left a unit with no configuration and no repository said what the file held. The unit
declaration now carries the document and the installer renders it. The control below is the same
request against a declaration with no document: it must yield none, which shows the rendering depends
on the declaration and not on the installer's own defaults.
"""

from __future__ import annotations

import copy
import json
import re
import stat
import subprocess
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
from omnimarket.nodes.node_silent_failure_alarm_effect.models import (
    ModelSilentFailureAlarmOptions,
)

pytestmark = pytest.mark.unit

UNIT = "silent-failure-alarm"
REVISION = "b" * 40


def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    if argv[-2:] == ["rev-parse", "HEAD"]:
        return subprocess.CompletedProcess(argv, 0, REVISION + "\n", "")
    if argv[-1] == "-u":
        return subprocess.CompletedProcess(argv, 0, "501\n", "")
    if argv[-1] == "-un":
        return subprocess.CompletedProcess(argv, 0, "fixture-user\n", "")
    return subprocess.CompletedProcess(argv, 0, "", "")


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
    ):
        monkeypatch.delenv(name, raising=False)


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


def _handler(*, declare: bool = True) -> HandlerRegistryUnits:
    units = copy.deepcopy(load_units())
    if not declare:
        units[UNIT].pop("config_file")
    return HandlerRegistryUnits(run=_run, units=units)


def test_alarm_config_dry_run_renders_the_declared_document(tmp_path: Path) -> None:
    result = _handler().handle(_request(tmp_path))
    assert result.exit_code == 0, result.stderr
    document = json.loads(result.config)
    root = tmp_path / "omni"
    appender = tmp_path / "appender"
    assert document["registry_root"] == str(root)
    assert document["ledger"] == str(root / "docs/tracking/ROLLING_WORK_LEDGER.md")
    assert document["ledger_lock"] == str(appender / ".venv/bin/onex-ledger")
    assert document["python"] == str(appender / ".venv/bin/python")
    assert document["host_table"] == str(tmp_path / "hosts.yaml")
    assert document["hook_events_command"][:6] == [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        "lab-user@runtime.example.test",
    ]
    assert "claude_hook_events" in document["hook_events_command"][6]
    assert document["drainer"]["home"] == str(tmp_path / "home")
    assert result.config_path == str(
        tmp_path / "home/.local/state/omni/silent-failure-alarm/config.json"
    )
    assert not Path(result.config_path).exists(), "a dry-run writes nothing"
    assert not re.search(r"\{[a-z_]+\}", result.config)


def test_alarm_config_request_ledger_path_is_the_ledger(tmp_path: Path) -> None:
    ledger = tmp_path / "state/ledger.md"
    result = _handler().handle(_request(tmp_path, ledger_path=str(ledger)))
    assert json.loads(result.config)["ledger"] == str(ledger)


def test_alarm_config_control_declaration_without_a_document_renders_none(
    tmp_path: Path,
) -> None:
    result = _handler(declare=False).handle(_request(tmp_path))
    assert result.exit_code == 0, result.stderr
    assert result.config == ""
    assert result.config_path == ""


def test_alarm_config_unfilled_placeholder_is_refused(tmp_path: Path) -> None:
    result = _handler().handle(_request(tmp_path, runtime_ssh_host=""))
    assert result.exit_code == 1
    assert "{runtime_ssh_host}" in result.stderr
    assert result.config == ""


def test_alarm_config_inputs_fall_back_to_the_shell_then_the_last_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ONEX_RUNTIME_SSH_HOST", "env-user@env.example.test")
    shell = _handler().handle(_request(tmp_path, runtime_ssh_host=""))
    assert shell.exit_code == 0, shell.stderr
    assert json.loads(shell.config)["hook_events_command"][5] == (
        "env-user@env.example.test"
    )
    monkeypatch.delenv("ONEX_RUNTIME_SSH_HOST")
    installed = _handler().handle(_request(tmp_path, mode="install"))
    assert installed.exit_code == 0, installed.stderr
    # A reinstall that names none of the inputs reproduces the file from the last install's record.
    bare = _request(
        tmp_path,
        mode="install",
        runtime_ssh_host="",
        appender_root="",
        host_table_path="",
    )
    again = _handler().handle(bare)
    assert again.exit_code == 0, again.stderr
    assert json.loads(again.config) == json.loads(installed.config)


def test_alarm_config_loads_as_options(tmp_path: Path) -> None:
    """The declaration cannot drift from what the alarm node accepts."""
    result = _handler().handle(_request(tmp_path))
    options = ModelSilentFailureAlarmOptions.model_validate(json.loads(result.config))
    assert options.registry_root == tmp_path / "omni"
    assert options.hook_events_command is not None
    assert options.hook_events_command[0] == "ssh"
    assert options.drainer is not None
    assert options.drainer.host == "h200"
    assert options.host_table is not None


def test_alarm_config_names_no_home_directory_or_person() -> None:
    document = json.dumps(load_units()[UNIT]["config_file"]["document"])
    for forbidden in ("/Users/", "/home/", "@192."):  # test-literal-ok
        assert forbidden not in document


def test_alarm_config_install_writes_the_file_at_0600_and_keeps_the_one_it_replaced(
    tmp_path: Path,
) -> None:
    target = tmp_path / "home/.local/state/omni/silent-failure-alarm/config.json"
    target.parent.mkdir(parents=True)
    target.write_text('{"registry_root": "hand-written"}\n')
    result = _handler().handle(_request(tmp_path, mode="install"))
    assert result.exit_code == 0, result.stderr
    assert json.loads(target.read_text()) == json.loads(result.config)
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    previous = target.with_name("config.json.previous")
    assert json.loads(previous.read_text()) == {"registry_root": "hand-written"}
    assert stat.S_IMODE(previous.stat().st_mode) == 0o600
    assert "kept the file it replaced" in result.stdout
    # A second install of the same declaration changes nothing and keeps the same previous copy.
    again = _handler().handle(_request(tmp_path, mode="install"))
    assert again.exit_code == 0, again.stderr
    assert json.loads(previous.read_text()) == {"registry_root": "hand-written"}


def test_alarm_config_install_writes_where_the_unit_reads_it(tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere/config.json"
    result = _handler().handle(
        _request(
            tmp_path,
            mode="install",
            path_overrides={"ONEX_SILENT_FAILURE_ALARM_CONFIG": str(elsewhere)},
        )
    )
    assert result.exit_code == 0, result.stderr
    assert elsewhere.is_file()
    assert result.config_path == str(elsewhere)
    assert str(elsewhere) in result.rendered


def test_alarm_config_install_refused_for_a_hole_writes_nothing(tmp_path: Path) -> None:
    result = _handler().handle(_request(tmp_path, mode="install", runtime_ssh_host=""))
    assert result.exit_code == 1
    target = tmp_path / "home/.local/state/omni/silent-failure-alarm/config.json"
    assert not target.exists()
