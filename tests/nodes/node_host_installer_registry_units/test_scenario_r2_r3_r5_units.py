# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_host_installer_registry_units: the R2, R3 and R5 scenario ticks install as systemd units (OMN-23533).

Unlike R4 and R6 (loopback stubs, no secret setting), these three scenarios read deployment settings and a probe key.
They sit in the environment file the node's own contract names (``schedule.environment_file``), which the unit loads
under the home directory with a leading dash; the unit itself never carries a value. The seam test reads each node's
contract and fails when the unit and the node disagree on the label, the environment file, the run limit or the
cadence; the control shows it failing for a changed cadence and a changed environment file.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units import (
    HandlerRegistryUnits,
    load_units,
)
from tests.nodes.node_host_installer_registry_units import (
    test_registry_units_systemd as base,
)

pytestmark = pytest.mark.unit

NODES = Path(__file__).resolve().parents[3] / "src" / "omnimarket" / "nodes"
CADENCE_SECONDS = {"hourly": 3600, "every_6h": 21600, "daily": 86400}
SENTINEL = "SENTINEL_SECRET_VALUE_7731"

UNITS = {
    "scenario-r2-systemd": {
        "node": "node_delegation_scenario_r2_effect",
        "tick": "runtime_scenario_r2_tick",
        "label": "onex-delegation-scenario-r2",
        "start_interval": 3600,
        "environment_file": ".config/onex/scenario-r2.env",
    },
    "scenario-r3-systemd": {
        "node": "node_delegation_scenario_r3_effect",
        "tick": "runtime_scenario_r3_tick",
        "label": "onex-delegation-scenario-r3",
        "start_interval": 21600,
        "environment_file": ".config/onex/scenario-r3.env",
    },
    "scenario-r5-systemd": {
        "node": "node_delegation_scenario_r5_effect",
        "tick": "runtime_scenario_r5_tick",
        "label": "onex-delegation-scenario-r5",
        "start_interval": 3600,
        "environment_file": ".config/onex/scenario-r5.env",
    },
}


def _entry(spec: dict[str, Any]) -> str:
    return f"src/omnimarket/nodes/{spec['node']}/{spec['tick']}.py"


def _module(spec: dict[str, Any]) -> str:
    return f"omnimarket.nodes.{spec['node']}.{spec['tick']}"


def _node_contract(spec: dict[str, Any]) -> dict[str, Any]:
    loaded = yaml.safe_load(
        (NODES / spec["node"] / "contract.yaml").read_text(encoding="utf-8")
    )
    assert isinstance(loaded, dict)
    return loaded


def seam_problems(
    unit_spec: dict[str, Any], node_contract: dict[str, Any]
) -> list[str]:
    """What the unit and its node's contract disagree on: label, environment file, run limit and cadence."""
    problems: list[str] = []
    schedule = node_contract["schedule"]
    if schedule["unit_name"] != unit_spec["label"]:
        problems.append(
            f"label {unit_spec['label']!r} is not the node's unit_name {schedule['unit_name']!r}"
        )
    if schedule.get("environment_file") != unit_spec.get("environment_file"):
        problems.append(
            f"environment_file {unit_spec.get('environment_file')!r} is not the node's {schedule.get('environment_file')!r}"
        )
    if schedule["timeout_seconds"] != unit_spec["schedule"].get("run_timeout_s"):
        problems.append(
            f"run_timeout_s {unit_spec['schedule'].get('run_timeout_s')!r} is not the node's timeout_seconds {schedule['timeout_seconds']!r}"
        )
    cadence = node_contract["runtime"]["cadence"]
    expected = CADENCE_SECONDS.get(cadence)
    if expected is None:
        problems.append(f"the node's cadence {cadence!r} has no interval in the table")
    elif expected != unit_spec["schedule"]["start_interval"]:
        problems.append(
            f"start_interval {unit_spec['schedule']['start_interval']} is not {expected} for cadence {cadence!r}"
        )
    return problems


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


def _install(tmp_path: Path, unit: str, **extra: Any) -> Any:
    spec = UNITS[unit]
    path, revision = _checkout(tmp_path, _entry(spec))
    return HandlerRegistryUnits(base.Recorder()).handle(
        base.request(
            tmp_path,
            unit=unit,
            mode="install",
            project_path=str(path),
            expected_revision=revision,
            path_overrides={},
            ledger_host_name="h201",
            workspace_config_root=str(tmp_path / "config"),
            **extra,
        )
    )


@pytest.mark.parametrize("unit", sorted(UNITS))
def test_the_unit_is_declared_on_systemd_with_its_tick_interval_and_environment_file(
    unit: str,
) -> None:
    spec = UNITS[unit]
    unit_spec = load_units()[unit]
    assert unit_spec["platform"] == "systemd"
    assert unit_spec["label"] == spec["label"]
    assert unit_spec["schedule"]["start_interval"] == spec["start_interval"]
    assert unit_spec["module"] == _module(spec)
    assert unit_spec["entry_file"] == _entry(spec)
    assert unit_spec["environment_file"] == spec["environment_file"]
    declared = str(
        {
            "environment": unit_spec.get("environment") or {},
            "environment_inputs": unit_spec.get("environment_inputs") or {},
        }
    )
    assert "ONEX_SCENARIO_" not in declared
    assert "ONEX_R3_" not in declared


@pytest.mark.parametrize("unit", sorted(UNITS))
def test_install_renders_the_timer_and_the_optional_environment_file_line(
    tmp_path: Path, unit: str
) -> None:
    spec = UNITS[unit]
    result = _install(tmp_path, unit)
    assert result.status == "success", result.stderr
    unit_dir = tmp_path / "home" / ".config/systemd/user"
    service = (unit_dir / f"{spec['label']}.service").read_text(encoding="utf-8")
    timer = (unit_dir / f"{spec['label']}.timer").read_text(encoding="utf-8")
    assert f"EnvironmentFile=-{tmp_path}/home/{spec['environment_file']}" in service
    assert "ONEX_SCENARIO_" not in service
    assert "ONEX_SCENARIO_" not in timer
    assert '"ONEX_HOST_NAME=h201"' in service
    assert f'"-m" "{_module(spec)}"' in service
    assert "TimeoutStartSec=900" in service
    assert f"OnUnitActiveSec={spec['start_interval']}" in timer
    assert f'"PATH={tmp_path}/omni/omnibase_infra/scripts:' in service


@pytest.mark.parametrize("unit", sorted(UNITS))
def test_environment_file_value_never_appears_in_the_unit(
    tmp_path: Path, unit: str
) -> None:
    spec = UNITS[unit]
    env_file = tmp_path / "home" / spec["environment_file"]
    env_file.parent.mkdir(parents=True, exist_ok=True)
    env_file.write_text(f"SOME_SETTING={SENTINEL}\n", encoding="utf-8")
    result = _install(tmp_path, unit)
    assert result.status == "success", result.stderr
    unit_dir = tmp_path / "home" / ".config/systemd/user"
    for suffix in ("service", "timer"):
        text = (unit_dir / f"{spec['label']}.{suffix}").read_text(encoding="utf-8")
        assert SENTINEL not in text
    assert SENTINEL not in result.rendered
    assert SENTINEL not in result.stdout + result.stderr


@pytest.mark.parametrize("unit", sorted(UNITS))
def test_an_install_without_the_required_host_name_is_refused(
    tmp_path: Path, unit: str
) -> None:
    """Error chain: the installer refuses an install that cannot name the host the ledger row writes."""
    spec = UNITS[unit]
    path, revision = _checkout(tmp_path, _entry(spec))
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


@pytest.mark.parametrize(
    "declared", ["../escape.env", "/etc/escape.env", ".config/onex/a b.env", "a;b.env"]
)
def test_a_traversal_or_unsafe_environment_file_is_refused(
    tmp_path: Path, declared: str
) -> None:
    """Error chain: an environment file that is absolute, climbs out of the home directory or is unsafe is refused."""
    unit_spec = copy.deepcopy(load_units()["scenario-r2-systemd"])
    unit_spec["environment_file"] = declared
    request = base.request(
        tmp_path,
        unit="scenario-r2-systemd",
        mode="dry-run",
        path_overrides={},
        ledger_host_name="h201",
        workspace_config_root=str(tmp_path / "config"),
    )
    with pytest.raises(ValueError, match="environment_file"):
        HandlerRegistryUnits(base.Recorder()).render_systemd(
            request, unit_spec, tmp_path / "project", "/usr/bin/uv", base.LINUX_PYTHON
        )


@pytest.mark.parametrize("unit", sorted(UNITS))
def test_unit_matches_the_node_contract(unit: str) -> None:
    """Seam: the unit and the scenario node's contract name the same unit, environment file, run limit and cadence."""
    spec = UNITS[unit]
    assert seam_problems(load_units()[unit], _node_contract(spec)) == []


def test_control_seam_problems_names_a_changed_cadence() -> None:
    spec = UNITS["scenario-r2-systemd"]
    node = copy.deepcopy(_node_contract(spec))
    node["runtime"]["cadence"] = "daily"
    problems = seam_problems(load_units()["scenario-r2-systemd"], node)
    assert any("cadence" in p and "86400" in p for p in problems)


def test_control_seam_problems_names_a_changed_environment_file() -> None:
    spec = UNITS["scenario-r3-systemd"]
    node = copy.deepcopy(_node_contract(spec))
    node["schedule"]["environment_file"] = ".config/onex/other.env"
    problems = seam_problems(load_units()["scenario-r3-systemd"], node)
    assert any("environment_file" in p for p in problems)
