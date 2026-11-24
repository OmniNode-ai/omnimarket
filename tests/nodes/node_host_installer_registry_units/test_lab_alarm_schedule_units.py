# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_host_installer_registry_units: the lab alarm's condition schedules are units an install loads.

The dev-tag-heartbeat, delegation-chain-canary, work-ledger-projection-stale, container-restarts and alarm-digest
schedules are declared in the lab alarm overlay (a unit name, a launchd label, an interval and a host), and the lab alarm
node renders and loads them with ``schedule install --condition``. Nothing ran that command, so none of them was ever
installed. Each is now a ``node_schedule`` unit of this installer: the
install hands the render, the load and the removal to the node's own scheduler, so the plist is written in one place
and carries the overlay's declared cadence.
"""

from __future__ import annotations

import plistlib
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
from omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units import (
    HandlerRegistryUnits,
    load_units,
)
from omnimarket.nodes.node_host_installer_registry_units.models.model_registry_units import (
    ModelRegistryUnitsRequest,
)
from omnimarket.nodes.node_lab_alarm import runtime_lab_alarm
from omnimarket.nodes.node_lab_alarm.handlers.schedule import render_launchd
from omnimarket.nodes.node_lab_alarm.overlay import ModelLabAlarmOverlay

pytestmark = pytest.mark.unit

HOST = "example-mac"
UNITS = {
    "lab-alarm-dev-tag-heartbeat": (
        "ai.omninode.lab-alarm-dev-tag-heartbeat",
        "dev-tag-heartbeat",
    ),
    "lab-alarm-delegation-chain-canary": (
        "ai.omninode.lab-alarm-delegation-chain-canary",
        "delegation-chain-canary",
    ),
    "lab-alarm-work-ledger-projection-stale": (
        "ai.omninode.lab-alarm-work-ledger-projection-stale",
        "work-ledger-projection-stale",
    ),
    "lab-alarm-container-restarts": (
        "ai.omninode.lab-alarm-container-restarts",
        "container-restarts",
    ),
    "lab-alarm-digest": ("ai.omninode.lab-alarm-digest", "alarm-digest"),
}
_NODE = Path(runtime_lab_alarm.__file__).parent


def _overlay() -> ModelLabAlarmOverlay:
    """The node's example overlay, with the two condition schedules carrying the labels the installer declares."""
    raw: dict[str, Any] = yaml.safe_load(
        (_NODE / "overlay.example.yaml").read_text(encoding="utf-8")
    )
    for label, verb in UNITS.values():
        raw["condition_schedules"][verb]["launchd_label"] = label
    return ModelLabAlarmOverlay.model_validate(raw)


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
    """The venv interpreter the overlay's host row names, so an install finds what the node checks for."""
    python = tmp_path / "omni" / "examplepkg/.venv/bin/python"
    python.parent.mkdir(parents=True)
    python.write_text("", encoding="utf-8")


@pytest.mark.parametrize(("unit", "declared"), sorted(UNITS.items()))
def test_the_registry_declares_the_lab_alarm_unit_the_overlay_schedules(
    unit: str, declared: tuple[str, str]
) -> None:
    label, verb = declared
    spec = load_units()[unit]
    assert spec["kind"] == "node_schedule"
    assert spec["platform"] == "launchd"
    assert spec["label"] == label
    assert spec["schedule_node"] == "node_lab_alarm"
    assert spec["schedule_condition"] == verb


@pytest.mark.parametrize(("unit", "declared"), sorted(UNITS.items()))
def test_a_dry_run_renders_the_nodes_own_plist_and_writes_nothing(
    tmp_path: Path, unit: str, declared: tuple[str, str]
) -> None:
    label, verb = declared
    run = _Runner()
    result = _handler(run).handle(_request(tmp_path, unit, mode="dry-run"))
    assert result.exit_code == 0, result.stderr
    overlay = _overlay()
    block = (overlay.condition_schedules or {})[verb]
    plist = plistlib.loads(result.stdout.encode("utf-8"))
    assert plist["Label"] == label
    assert plist["StartInterval"] == block.interval_seconds
    assert plist["ProgramArguments"][-2:] == [
        "omnimarket.nodes.node_lab_alarm",
        verb,
    ]
    host = block.host(HOST)
    assert host is not None
    assert (
        result.stdout.encode("utf-8").strip()
        == render_launchd(
            overlay.model_copy(update={"schedule": block}),
            host,
            omni_root=tmp_path / "omni",
            home=tmp_path / "home",
            overlay_root=tmp_path / "omni" / "overlays/overlays",
            verb=verb,
        ).strip()
    )
    assert run.commands == []
    assert not (tmp_path / "home").exists()


@pytest.mark.parametrize(("unit", "declared"), sorted(UNITS.items()))
def test_an_install_writes_the_nodes_plist_and_loads_it(
    tmp_path: Path, unit: str, declared: tuple[str, str]
) -> None:
    label, _ = declared
    _registry(tmp_path)
    run = _Runner()
    result = _handler(run).handle(_request(tmp_path, unit, mode="install"))
    assert result.exit_code == 0, result.stderr
    plist = tmp_path / "home/Library/LaunchAgents" / f"{label}.plist"
    assert plistlib.loads(plist.read_bytes())["Label"] == label
    assert any(command[:2] == ["launchctl", "bootstrap"] for command in run.commands)


def test_an_uninstall_unloads_the_unit_and_removes_its_plist(tmp_path: Path) -> None:
    unit = "lab-alarm-container-restarts"
    _registry(tmp_path)
    run = _Runner()
    _handler(run).handle(_request(tmp_path, unit, mode="install"))
    result = _handler(run).handle(_request(tmp_path, unit, mode="uninstall"))
    assert result.exit_code == 0, result.stderr
    assert not (
        tmp_path
        / "home/Library/LaunchAgents/ai.omninode.lab-alarm-container-restarts.plist"
    ).exists()
    assert any(command[:2] == ["launchctl", "bootout"] for command in run.commands)


def test_a_request_that_names_no_host_is_refused(tmp_path: Path) -> None:
    run = _Runner()
    result = _handler(run).handle(
        _request(tmp_path, "lab-alarm-digest", mode="dry-run", host_name="")
    )
    assert result.exit_code == 1
    assert "host_name" in result.stderr
    assert run.commands == []


def test_a_host_the_overlay_does_not_schedule_is_refused(tmp_path: Path) -> None:
    run = _Runner()
    result = _handler(run).handle(
        _request(tmp_path, "lab-alarm-digest", mode="dry-run", host_name="elsewhere")
    )
    assert result.exit_code == 1
    assert "elsewhere" in result.stderr
    assert run.commands == []


def test_a_rollback_is_refused_because_the_node_keeps_no_previous_install(
    tmp_path: Path,
) -> None:
    run = _Runner()
    result = _handler(run).handle(
        _request(tmp_path, "lab-alarm-digest", mode="rollback")
    )
    assert result.exit_code == 1
    assert "uninstall" in result.stderr
    assert run.commands == []
