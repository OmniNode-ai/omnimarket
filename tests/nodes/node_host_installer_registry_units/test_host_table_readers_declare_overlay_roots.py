# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Every installed unit whose program reads the lab host table declares ONEX_SKILL_OVERLAY_ROOTS (OMN-23950).

The shared host table reader (``resolve_lab_hosts_table``) reads the table through the overlay roots alone since
omnimarket#5900, and launchd does not inherit the installing shell's environment. The silent-failure alarm unit declared
only ONEX_WORKSPACE_CONFIG_ROOT, so after that change its tick went from exit 0 to exit 3 on the launching Mac (integration
pass 9): it could not read which hosts are unavailable. The control below is derived from the program's own source, not
from a list of unit names: a unit whose module package calls the reader must declare the roots, in its environment and as
an overridable path, and its rendered plist must carry the overlays clone the host's row declares first.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any

import pytest

import omnimarket
from omnimarket.models.overlay_keys import (
    ModelLabHostClone,
    ModelLabHostRow,
    ModelLabHostsTable,
)
from omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units import (
    HandlerRegistryUnits,
    load_units,
)
from tests.nodes.node_host_installer_registry_units.test_registry_units import (
    BREW_PYTHON,
    Recorder,
    make_checkout,
    plist_of,
    request,
)

pytestmark = pytest.mark.unit

ROOTS = "ONEX_SKILL_OVERLAY_ROOTS"
READER = re.compile(r"\bresolve_lab_hosts_table\b|\bread_unavailable_hosts\b")
SOURCE_ROOT = Path(omnimarket.__file__).parent


def module_package(module: str) -> Path:
    """The directory of the node package a unit's module lives in (``omnimarket.nodes.<node>[...]``)."""
    parts = module.split(".")
    assert parts[:2] == ["omnimarket", "nodes"], module
    return SOURCE_ROOT / "nodes" / parts[2]


def reads_host_table(module: str) -> bool:
    return any(
        READER.search(path.read_text(encoding="utf-8"))
        for path in module_package(module).rglob("*.py")
        if "tests" not in path.parts
    )


def reader_units() -> dict[str, dict[str, Any]]:
    return {
        name: unit
        for name, unit in load_units().items()
        if str(unit.get("module", "")).startswith("omnimarket.nodes.")
        and reads_host_table(unit["module"])
    }


def test_control_the_scan_finds_the_silent_failure_alarm_among_the_readers() -> None:
    assert "silent-failure-alarm" in reader_units()


def test_golden_every_reader_unit_declares_the_roots_by_default_and_as_an_override() -> (
    None
):
    """Failing control: on the base contract the silent-failure alarm declared neither."""
    undeclared = sorted(
        name
        for name, unit in reader_units().items()
        if ROOTS not in (unit.get("environment") or {})
        or ROOTS not in (unit.get("environment_path_overrides") or [])
        or ROOTS not in (unit.get("environment_refuse_worktree_paths") or [])
    )
    assert undeclared == []


@pytest.fixture(autouse=True)
def brew_python(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units.interpreter_present",
        lambda path: path == BREW_PYTHON,
    )
    monkeypatch.setattr(
        shutil, "which", lambda name: "/usr/bin/uv" if name == "uv" else None
    )


def launchd_reader_units() -> list[str]:
    return sorted(
        name
        for name, unit in reader_units().items()
        if unit.get("platform", "launchd") == "launchd"
    )


@pytest.mark.parametrize("name", launchd_reader_units())
def test_golden_the_rendered_plist_names_the_overlays_clone_first(
    tmp_path: Path, name: str
) -> None:
    unit = load_units()[name]
    make_checkout(tmp_path / "omni" / "omnimarket", entry=unit["entry_file"])
    table = ModelLabHostsTable(
        hosts=(
            ModelLabHostRow(
                name="h200",
                target="h200.lab",
                clones=(
                    ModelLabHostClone(
                        repo="OmniNode-ai/overlays",
                        ref="main",
                        path="$OMNI_HOME/../overlays",
                    ),
                ),
            ),
        )
    )
    overrides = {
        variable: "h200"
        for variable in unit.get("environment_path_overrides") or []
        if variable == "LAB_WORK_HOST_NAME"
    }
    result = HandlerRegistryUnits(Recorder(), hosts=lambda: table).handle(
        request(
            tmp_path,
            unit=name,
            mode="dry-run",
            host_name="h200",
            marketplace_root=str(tmp_path / "market"),
            appender_root=str(tmp_path / "appender"),
            host_table_path=str(tmp_path / "hosts.yaml"),
            path_overrides=overrides,
        )
    )
    assert result.status == "success", result.stderr
    roots = plist_of(result.rendered)["EnvironmentVariables"][ROOTS].split(":")
    assert roots[0] == f"{tmp_path}/overlays/overlays"
    assert all("omni_worktrees" not in root for root in roots)
