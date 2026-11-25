# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_host_installer_registry_units: the delegation-check-probe unit carries the lane bus environment by reference (OMN-26043).

Measured on h051: the installed unit rendered no broker environment, so the probe's event was not delivered
(KAFKA_BOOTSTRAP_SERVERS unset). The other registry units reach the bus through the lane declaration table found under
OMNIBASE_PATH and ONEX_WORKSPACE_CONFIG_ROOT; no broker address is written into a unit.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units import (
    HandlerRegistryUnits,
    load_units,
)
from tests.nodes.node_host_installer_registry_units import (
    test_registry_units_systemd as base,
)

UNIT = "delegation-check-probe"
LABEL = "onex-delegation-check-probe"
ENTRY = "src/omnimarket/nodes/node_delegation_check_probe_effect/runtime_delegation_check_probe_tick.py"
BUS_ENV = ("OMNIBASE_PATH", "ONEX_WORKSPACE_CONFIG_ROOT")

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def linux_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        f"{base.HANDLER}.interpreter_present", lambda path: path == base.LINUX_PYTHON
    )
    monkeypatch.setattr(
        "shutil.which", lambda name: "/usr/bin/uv" if name == "uv" else None
    )


def _checkout(tmp_path: Path, entry: str = ENTRY) -> tuple[Path, str]:
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


@pytest.mark.parametrize("name", BUS_ENV)
def test_the_declared_unit_names_the_lane_bus_environment_by_reference(
    name: str,
) -> None:
    """A unit that declares neither variable is refused: no broker address is hard-coded, the lane table is found by reference."""
    environment = load_units()[UNIT]["environment"]
    assert name in environment, f"{UNIT} declares no {name}"
    assert environment[name] in ("{registry_root}", "{workspace_config_root}")
    assert "KAFKA_BOOTSTRAP_SERVERS" not in environment


def test_the_declaration_matches_the_other_registry_units() -> None:
    units = load_units()
    reference = units["overlay-store-load-systemd"]["environment"]
    for name in BUS_ENV:
        assert units[UNIT]["environment"][name] == reference[name]


def test_the_rendered_service_carries_the_bus_environment(tmp_path: Path) -> None:
    path, revision = _checkout(tmp_path)
    result = HandlerRegistryUnits(base.Recorder()).handle(
        base.request(
            tmp_path,
            unit=UNIT,
            mode="install",
            project_path=str(path),
            expected_revision=revision,
            path_overrides={},
            workspace_config_root=str(tmp_path / "config-root"),
            marketplace_root=str(tmp_path / "market"),
        )
    )
    assert result.status == "success", result.stderr
    service = (tmp_path / "home/.config/systemd/user" / f"{LABEL}.service").read_text(
        encoding="utf-8"
    )
    assert f'"OMNIBASE_PATH={tmp_path}/omni"' in service
    assert f'"ONEX_WORKSPACE_CONFIG_ROOT={tmp_path}/config-root"' in service
    assert "KAFKA_BOOTSTRAP_SERVERS" not in service


def test_another_units_render_is_unchanged(tmp_path: Path) -> None:
    """The overlay-store-load unit renders the same two bus variables it rendered before this change, and no more."""
    entry = "src/omnimarket/nodes/node_overlay_store_load_effect/runtime_overlay_store_load_tick.py"
    path, revision = _checkout(tmp_path, entry)
    result = HandlerRegistryUnits(base.Recorder()).handle(
        base.request(
            tmp_path,
            unit="overlay-store-load-systemd",
            mode="install",
            project_path=str(path),
            expected_revision=revision,
            path_overrides={},
            workspace_config_root=str(tmp_path / "config-root"),
        )
    )
    assert result.status == "success", result.stderr
    service = (
        tmp_path / "home/.config/systemd/user/onex-overlay-store-load.service"
    ).read_text(encoding="utf-8")
    assert f'"OMNIBASE_PATH={tmp_path}/omni"' in service
    assert f'"ONEX_WORKSPACE_CONFIG_ROOT={tmp_path}/config-root"' in service
    assert "ONEX_STATE_DIR" not in service


SNAPSHOT_ROOT = ".local/state/onex/overlay-store"


def test_the_declared_unit_reads_the_overlay_store_snapshot_root() -> None:
    """The reader keeps delegation_check_responders and delegation_check_gateway in the snapshot directory; a unit whose
    overlay roots omit it finds neither row and every check reads PRECONDITION-ABSENT (measured on h051, 2026-11-25)."""
    appended = load_units()[UNIT]["environment_append_roots"][
        "ONEX_SKILL_OVERLAY_ROOTS"
    ]
    assert appended == ["{home}/" + SNAPSHOT_ROOT]


def test_the_rendered_service_names_the_snapshot_root_last(
    tmp_path: Path,
) -> None:
    path, revision = _checkout(tmp_path)
    result = HandlerRegistryUnits(base.Recorder()).handle(
        base.request(
            tmp_path,
            unit=UNIT,
            mode="install",
            project_path=str(path),
            expected_revision=revision,
            path_overrides={},
            workspace_config_root=str(tmp_path / "config-root"),
            marketplace_root=str(tmp_path / "market"),
        )
    )
    assert result.status == "success", result.stderr
    service = (tmp_path / "home/.config/systemd/user" / f"{LABEL}.service").read_text(
        encoding="utf-8"
    )
    snapshot = f"{tmp_path}/home/{SNAPSHOT_ROOT}"
    roots = next(
        line.split("ONEX_SKILL_OVERLAY_ROOTS=")[1].split('"')[0]
        for line in service.splitlines()
        if "ONEX_SKILL_OVERLAY_ROOTS=" in line
    ).split(":")
    assert snapshot in roots
    assert roots.index(snapshot) > 0
