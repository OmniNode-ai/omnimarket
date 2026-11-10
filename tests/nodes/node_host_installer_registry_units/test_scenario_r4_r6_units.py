# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_host_installer_registry_units: the R4 and R6 scenario ticks install as systemd units that hold no secret setting.

R4 and R6 use loopback stubs, so their units carry no environment file and no ONEX_SCENARIO_RN_ variable; the
settings the tick needs are the host's, found under ONEX_WORKSPACE_CONFIG_ROOT, and the host name the ledger row
names, from the request.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units import (
    HandlerRegistryUnits,
    load_units,
)
from tests.nodes.node_host_installer_registry_units import (
    test_registry_units_systemd as base,
)

UNITS = {
    "scenario-r4-systemd": {
        "label": "onex-delegation-scenario-r4",
        "entry": "src/omnimarket/nodes/node_delegation_scenario_r4_effect/runtime_scenario_r4_tick.py",
        "module": "omnimarket.nodes.node_delegation_scenario_r4_effect.runtime_scenario_r4_tick",
        "start_interval": 21600,
    },
    "scenario-r6-systemd": {
        "label": "onex-delegation-scenario-r6",
        "entry": "src/omnimarket/nodes/node_delegation_scenario_r6_effect/runtime_scenario_r6_tick.py",
        "module": "omnimarket.nodes.node_delegation_scenario_r6_effect.runtime_scenario_r6_tick",
        "start_interval": 86400,
    },
}

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def linux_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        f"{base.HANDLER}.interpreter_present", lambda path: path == base.LINUX_PYTHON
    )
    monkeypatch.setattr(
        "shutil.which", lambda name: "/usr/bin/uv" if name == "uv" else None
    )


def _checkout(tmp_path: Path, entry: str) -> tuple[Path, str]:
    path = tmp_path / "branch"
    # make_checkout writes base.ENTRY; the scenario tick's entry file is added and committed over it
    base.make_checkout(path)
    (path / entry).parent.mkdir(parents=True, exist_ok=True)
    (path / entry).write_text("print('tick')\n", encoding="utf-8")
    base._git(["git", "-C", str(path), "add", "-A"], check=True)
    base._git(
        [
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
            "y",
        ],
        check=True,
    )
    revision = base._git(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    return path, revision


@pytest.mark.parametrize("unit", sorted(UNITS))
def test_the_unit_is_declared_on_systemd_and_holds_no_scenario_variable(
    unit: str,
) -> None:
    spec = UNITS[unit]
    unit_spec = load_units()[unit]
    assert unit_spec["platform"] == "systemd"
    assert unit_spec["schedule"]["start_interval"] == spec["start_interval"]
    assert unit_spec["module"] == spec["module"]
    assert unit_spec["entry_file"] == spec["entry"]
    assert "environment_file" not in unit_spec
    declared = json.dumps(
        {
            "environment": unit_spec.get("environment") or {},
            "environment_inputs": unit_spec.get("environment_inputs") or {},
            "required_environment_inputs": unit_spec.get("required_environment_inputs")
            or [],
        }
    )
    assert "ONEX_SCENARIO_" not in declared


@pytest.mark.parametrize("unit", sorted(UNITS))
def test_install_renders_the_timer_and_holds_no_environment_file_line(
    tmp_path: Path, unit: str
) -> None:
    spec = UNITS[unit]
    path, revision = _checkout(tmp_path, spec["entry"])
    run = base.Recorder()
    request = base.request(
        tmp_path,
        unit=unit,
        mode="install",
        project_path=str(path),
        expected_revision=revision,
        path_overrides={},
        ledger_host_name="h201",
        workspace_config_root=str(tmp_path / "config"),
    )
    result = HandlerRegistryUnits(run).handle(request)
    assert result.status == "success", result.stderr
    unit_dir = tmp_path / "home" / ".config/systemd/user"
    service = (unit_dir / f"{spec['label']}.service").read_text(encoding="utf-8")
    timer = (unit_dir / f"{spec['label']}.timer").read_text(encoding="utf-8")
    assert "EnvironmentFile" not in service
    assert "ONEX_SCENARIO_" not in service
    assert "ONEX_SCENARIO_" not in timer
    assert '"ONEX_HOST_NAME=h201"' in service
    assert f'"ONEX_WORKSPACE_CONFIG_ROOT={tmp_path}/config"' in service
    assert f'"-m" "{spec["module"]}"' in service
    assert "TimeoutStartSec=900" in service
    assert f"OnUnitActiveSec={spec['start_interval']}" in timer
    # The scenario runs `onex` by name: the registry's launcher directory leads the unit's search path.
    assert f'"PATH={tmp_path}/omni/omnibase_infra/scripts:' in service


@pytest.mark.parametrize("unit", sorted(UNITS))
def test_an_install_without_the_required_host_name_is_refused(
    tmp_path: Path, unit: str
) -> None:
    """Error chain: the installer refuses an install that cannot name the host the ledger row writes."""
    spec = UNITS[unit]
    path, revision = _checkout(tmp_path, spec["entry"])
    result = HandlerRegistryUnits(base.Recorder()).handle(
        base.request(
            tmp_path,
            unit=unit,
            mode="install",
            project_path=str(path),
            expected_revision=revision,
            path_overrides={},
            workspace_config_root=str(tmp_path / "config"),
        )
    )
    assert result.status == "failure"
    assert not (
        tmp_path / "home/.config/systemd/user" / f"{spec['label']}.timer"
    ).exists()


@pytest.mark.parametrize("unit", sorted(UNITS))
def test_a_checkout_without_the_tick_entry_file_is_refused(
    tmp_path: Path, unit: str
) -> None:
    """Error chain: the installer refuses a branch that does not carry the scenario tick."""
    spec = UNITS[unit]
    path = tmp_path / "branch"
    base.make_checkout(path)
    result = HandlerRegistryUnits(base.Recorder()).handle(
        base.request(
            tmp_path,
            unit=unit,
            mode="install",
            project_path=str(path),
            path_overrides={},
        )
    )
    assert result.status == "failure"
    assert not (
        tmp_path / "home/.config/systemd/user" / f"{spec['label']}.timer"
    ).exists()
