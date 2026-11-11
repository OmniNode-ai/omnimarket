# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_host_installer_registry_units: the R2 scenario tick installs as a systemd unit whose settings stay out of it (OMN-23432).

The scenario's settings live in the secret store, read through the host's machine identity, so the unit carries
only an optional EnvironmentFile the host may fill with overrides — never a ONEX_SCENARIO_R2_ variable.
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

UNIT = "scenario-r2-systemd"
LABEL = "onex-delegation-scenario-r2"
ENTRY = "src/omnimarket/nodes/node_delegation_scenario_r2_effect/runtime_scenario_r2_tick.py"
ENVIRONMENT_FILE = ".config/onex/scenario-r2.env"

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def linux_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        f"{base.HANDLER}.interpreter_present", lambda path: path == base.LINUX_PYTHON
    )
    monkeypatch.setattr(
        "shutil.which", lambda name: "/usr/bin/uv" if name == "uv" else None
    )


def _checkout(tmp_path: Path) -> tuple[Path, str]:
    path = tmp_path / "branch"
    # make_checkout writes base.ENTRY; the scenario tick's entry file is added and committed over it
    base.make_checkout(path)
    (path / ENTRY).parent.mkdir(parents=True, exist_ok=True)
    (path / ENTRY).write_text("print('tick')\n", encoding="utf-8")
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


def test_the_unit_is_declared_hourly_on_systemd_and_holds_no_scenario_variable() -> (
    None
):
    unit = load_units()[UNIT]
    assert unit["platform"] == "systemd"
    assert unit["schedule"]["start_interval"] == 3600
    assert unit["module"].endswith("runtime_scenario_r2_tick")
    assert unit["environment_file"] == ENVIRONMENT_FILE
    declared = json.dumps(
        {
            "environment": unit.get("environment") or {},
            "environment_inputs": unit.get("environment_inputs") or {},
            "required_environment_inputs": unit.get("required_environment_inputs")
            or [],
        }
    )
    assert "ONEX_SCENARIO_R2_" not in declared


def test_install_renders_the_environment_file_line_and_an_hourly_timer(
    tmp_path: Path,
) -> None:
    path, revision = _checkout(tmp_path)
    run = base.Recorder()
    request = base.request(
        tmp_path,
        unit=UNIT,
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
    service = (unit_dir / f"{LABEL}.service").read_text(encoding="utf-8")
    timer = (unit_dir / f"{LABEL}.timer").read_text(encoding="utf-8")
    assert f"EnvironmentFile=-{tmp_path}/home/{ENVIRONMENT_FILE}" in service
    assert '"ONEX_HOST_NAME=h201"' in service
    assert f'"ONEX_WORKSPACE_CONFIG_ROOT={tmp_path}/config"' in service
    assert (
        '"-m" "omnimarket.nodes.node_delegation_scenario_r2_effect.runtime_scenario_r2_tick"'
        in service
    )
    assert "TimeoutStartSec=900" in service
    assert "OnUnitActiveSec=3600" in timer
    assert "ONEX_SCENARIO_R2_" not in service
    assert "ONEX_SCENARIO_R2_" not in timer
    # The scenario runs `onex` by name: the registry's launcher directory leads the unit's search path.
    assert f'"PATH={tmp_path}/omni/omnibase_infra/scripts:' in service


def test_a_unit_that_declares_no_environment_file_renders_no_environment_file_line(
    tmp_path: Path,
) -> None:
    service, _timer = HandlerRegistryUnits(base.Recorder()).render_systemd(
        base.request(tmp_path, unit="overlay-store-load-systemd", path_overrides={}),
        load_units()["overlay-store-load-systemd"],
        tmp_path / "omni",
        "/usr/bin/uv",
        base.LINUX_PYTHON,
    )
    assert "EnvironmentFile" not in service


def test_an_install_without_the_required_host_name_is_refused(tmp_path: Path) -> None:
    """Error chain: the installer refuses an install that cannot name the host the ledger row writes."""
    path, revision = _checkout(tmp_path)
    result = HandlerRegistryUnits(base.Recorder()).handle(
        base.request(
            tmp_path,
            unit=UNIT,
            mode="install",
            project_path=str(path),
            expected_revision=revision,
            path_overrides={},
            workspace_config_root=str(tmp_path / "config"),
        )
    )
    assert result.status == "failure"
    assert not (tmp_path / "home/.config/systemd/user" / f"{LABEL}.timer").exists()


def test_an_environment_file_outside_the_home_is_refused(tmp_path: Path) -> None:
    unit = load_units()[UNIT]
    handler = HandlerRegistryUnits(base.Recorder())
    request = base.request(
        tmp_path,
        unit=UNIT,
        path_overrides={},
        workspace_config_root=str(tmp_path / "config"),
    )
    for declared in ("../x", "/etc/scenario-r2.env"):
        with pytest.raises(ValueError, match="relative to the home"):
            handler.render_systemd(
                request,
                unit | {"environment_file": declared},
                tmp_path / "omni",
                "/usr/bin/uv",
                base.LINUX_PYTHON,
            )


def test_a_launchd_unit_declaring_an_environment_file_is_refused(
    tmp_path: Path,
) -> None:
    unit = load_units()["overlay-store-refresh"]
    with pytest.raises(ValueError, match="launchd units do not support"):
        HandlerRegistryUnits(base.Recorder()).render(
            base.request(tmp_path, unit="overlay-store-refresh"),
            unit | {"environment_file": ENVIRONMENT_FILE},
            tmp_path / "omni",
            "/usr/bin/uv",
            base.LINUX_PYTHON,
        )


def test_a_checkout_without_the_tick_entry_file_is_refused(tmp_path: Path) -> None:
    """Error chain: the installer refuses a branch that does not carry the scenario tick."""
    path = tmp_path / "branch"
    base.make_checkout(path)
    result = HandlerRegistryUnits(base.Recorder()).handle(
        base.request(
            tmp_path,
            unit=UNIT,
            mode="install",
            project_path=str(path),
            path_overrides={},
        )
    )
    assert result.status == "failure"
    assert not (tmp_path / "home/.config/systemd/user" / f"{LABEL}.timer").exists()


def test_an_install_without_the_lane_config_root_is_refused(tmp_path: Path) -> None:
    """Error chain: the ledger append opens the lane bus from the config root, so an install that names none is refused."""
    path, revision = _checkout(tmp_path)
    result = HandlerRegistryUnits(base.Recorder()).handle(
        base.request(
            tmp_path,
            unit=UNIT,
            mode="install",
            project_path=str(path),
            expected_revision=revision,
            path_overrides={},
            ledger_host_name="h201",
        )
    )
    assert result.status == "failure"
    assert "workspace_config_root" in result.stderr
    assert not (tmp_path / "home/.config/systemd/user" / f"{LABEL}.timer").exists()
