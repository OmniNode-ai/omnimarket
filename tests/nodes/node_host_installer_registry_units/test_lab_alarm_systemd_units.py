# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_host_installer_registry_units: every lab alarm condition schedule has a systemd variant.

The lab alarm conditions were declared for launchd only, so ``onex-host-install-units --list-units`` offered a systemd
user host (the Linux lab hosts) none of them and nothing installed them there. Each launchd ``node_schedule`` unit has a
``-systemd`` twin that names the same node and condition and carries the overlay's systemd unit name as its label; the
node's own scheduler renders the service and the timer for a host the overlay lists with a ``systemd-user`` scheduler.
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.models.overlay_keys import (
    ModelLabHostClone,
    ModelLabHostRow,
    ModelLabHostsTable,
)
from omnimarket.nodes.node_host_installer_registry_units import (
    runtime_registry_units_cli as cli,
)
from omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units import (
    HandlerRegistryUnits,
    load_units,
)
from omnimarket.nodes.node_host_installer_registry_units.models.model_registry_units import (
    ModelRegistryUnitsRequest,
)
from omnimarket.nodes.node_lab_alarm import runtime_lab_alarm
from omnimarket.nodes.node_lab_alarm.overlay import ModelLabAlarmOverlay

pytestmark = pytest.mark.unit

HOST = "example-linux"
#: Each launchd unit, its systemd twin, the label the twin declares, and the condition verb.
LAB_ALARM_SYSTEMD_UNITS = {
    "lab-alarm-dev-tag-heartbeat": (
        "lab-alarm-dev-tag-heartbeat-systemd",
        "lab-alarm-dev-tag-heartbeat",
        "dev-tag-heartbeat",
    ),
    "lab-alarm-delegation-chain-canary": (
        "lab-alarm-delegation-chain-canary-systemd",
        "lab-alarm-delegation-chain-canary",
        "delegation-chain-canary",
    ),
    "lab-alarm-work-ledger-projection-stale": (
        "lab-alarm-work-ledger-projection-stale-systemd",
        "lab-alarm-work-ledger-projection-stale",
        "work-ledger-projection-stale",
    ),
    "lab-alarm-container-restarts": (
        "lab-alarm-container-restarts-systemd",
        "lab-alarm-container-restarts",
        "container-restarts",
    ),
    "lab-alarm-digest": (
        "lab-alarm-digest-systemd",
        "lab-alarm-digest",
        "alarm-digest",
    ),
}
_NODE = Path(runtime_lab_alarm.__file__).parent


def _overlay() -> ModelLabAlarmOverlay:
    """The node's example overlay: each condition lists a systemd-user host beside its launchd one."""
    raw: dict[str, Any] = yaml.safe_load(
        (_NODE / "overlay.example.yaml").read_text(encoding="utf-8")
    )
    return ModelLabAlarmOverlay.model_validate(raw)


def _installed_name(verb: str) -> str:
    """The unit name the node's scheduler gives the service and the timer: the overlay's, never the label."""
    return (_overlay().condition_schedules or {})[verb].unit_name


class _Runner:
    def __init__(self) -> None:
        self.commands: list[list[str]] = []

    def __call__(self, command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        self.commands.append(list(command))
        return subprocess.CompletedProcess(list(command), 0, "ok\n", "")


def _hosts() -> ModelLabHostsTable:
    clone = ModelLabHostClone(
        repo="OmniNode-ai/overlays", ref="main", path="$OMNI_HOME/overlays"
    )
    return ModelLabHostsTable(
        hosts=(ModelLabHostRow(name=HOST, target=f"{HOST}.lab", clones=(clone,)),)
    )


def _handler(run: _Runner) -> HandlerRegistryUnits:
    return HandlerRegistryUnits(run, hosts=_hosts, schedule_overlay=_overlay)


def _request(tmp_path: Path, unit: str, **fields: Any) -> ModelRegistryUnitsRequest:
    values: dict[str, Any] = {
        "unit": unit,
        "home": str(tmp_path / "home"),
        "registry_root": str(tmp_path / "omni"),
        "host_name": HOST,
        **fields,
    }
    return ModelRegistryUnitsRequest(**values)


def _registry(tmp_path: Path) -> None:
    python = tmp_path / "omni" / "examplepkg/.venv/bin/python"
    python.parent.mkdir(parents=True)
    python.write_text("", encoding="utf-8")


def test_list_units_lists_every_lab_alarm_unit_as_systemd(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.main(["--list-units"]) == 0
    listed = {
        parts[0]: parts[1]
        for parts in (line.split() for line in capsys.readouterr().out.splitlines())
    }
    for twin, _, _ in LAB_ALARM_SYSTEMD_UNITS.values():
        assert listed.get(twin) == "systemd", f"{twin} is not listed as systemd"


@pytest.mark.parametrize(
    ("launchd_unit", "declared"), sorted(LAB_ALARM_SYSTEMD_UNITS.items())
)
def test_the_systemd_twin_names_the_same_node_condition_as_the_launchd_unit(
    launchd_unit: str, declared: tuple[str, str, str]
) -> None:
    twin, label, verb = declared
    units = load_units()
    spec = units[twin]
    assert spec["platform"] == "systemd"
    assert spec["kind"] == "node_schedule"
    assert spec["label"] == label
    assert spec["schedule_node"] == units[launchd_unit]["schedule_node"]
    assert (
        spec["schedule_condition"] == verb == units[launchd_unit]["schedule_condition"]
    )
    assert spec["state_directory"] == units[launchd_unit]["state_directory"]


@pytest.mark.parametrize(
    ("launchd_unit", "declared"), sorted(LAB_ALARM_SYSTEMD_UNITS.items())
)
def test_a_dry_run_on_a_systemd_host_renders_the_service_and_the_timer(
    tmp_path: Path, launchd_unit: str, declared: tuple[str, str, str]
) -> None:
    twin, _, verb = declared
    run = _Runner()
    result = _handler(run).handle(_request(tmp_path, twin, mode="dry-run"))
    assert result.exit_code == 0, result.stderr
    block = (_overlay().condition_schedules or {})[verb]
    assert "[Service]" in result.stdout
    assert "[Timer]" in result.stdout
    assert f"-m omnimarket.nodes.node_lab_alarm {verb}\n" in result.stdout
    assert f"OnUnitActiveSec={block.interval_seconds}s" in result.stdout
    assert f"Unit={_installed_name(verb)}.service" in result.stdout
    assert run.commands == []
    assert not (tmp_path / "home").exists()


@pytest.mark.parametrize(
    ("launchd_unit", "declared"), sorted(LAB_ALARM_SYSTEMD_UNITS.items())
)
def test_an_install_on_a_systemd_host_writes_both_files_and_enables_the_timer(
    tmp_path: Path, launchd_unit: str, declared: tuple[str, str, str]
) -> None:
    twin, _, verb = declared
    label = _installed_name(verb)
    _registry(tmp_path)
    run = _Runner()
    result = _handler(run).handle(_request(tmp_path, twin, mode="install"))
    assert result.exit_code == 0, result.stderr
    unit_dir = tmp_path / "home/.config/systemd/user"
    assert (unit_dir / f"{label}.service").is_file()
    assert (unit_dir / f"{label}.timer").is_file()
    assert ["systemctl", "--user", "enable", "--now", f"{label}.timer"] in run.commands
    assert not any(command[0] == "launchctl" for command in run.commands)


def test_an_uninstall_on_a_systemd_host_disables_the_timer_and_removes_both_files(
    tmp_path: Path,
) -> None:
    twin, _, verb = LAB_ALARM_SYSTEMD_UNITS["lab-alarm-container-restarts"]
    label = _installed_name(verb)
    _registry(tmp_path)
    run = _Runner()
    _handler(run).handle(_request(tmp_path, twin, mode="install"))
    result = _handler(run).handle(_request(tmp_path, twin, mode="uninstall"))
    assert result.exit_code == 0, result.stderr
    unit_dir = tmp_path / "home/.config/systemd/user"
    assert not (unit_dir / f"{label}.service").exists()
    assert not (unit_dir / f"{label}.timer").exists()
    assert ["systemctl", "--user", "disable", "--now", f"{label}.timer"] in run.commands
