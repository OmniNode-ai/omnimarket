# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The live profile's tick unit carries the ledger append command, the workspace config root and the overlay roots.

The append command is built from the fill host's own row of the lab host table (its ledger bus lane and interpreter);
the config root and the overlay roots are what the installing host declares. A value that is not declared refuses the
install naming it.
"""

from __future__ import annotations

import shlex
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from omnimarket.models.overlay_keys.model_overlay_documents import ModelLabHostRow
from omnimarket.nodes.node_floor_fill_orchestrator.models import (
    load_floor_fill_schedule,
)
from omnimarket.nodes.node_floor_fill_orchestrator.runtime_floor_fill_schedule import (
    run_schedule,
)
from omnimarket.nodes.node_floor_fill_orchestrator.tests.support import OVERLAY
from omnimarket.nodes.node_floor_fill_orchestrator.tests.test_floor_fill_live_binding import (
    live_section,
)

pytestmark = pytest.mark.unit

HOSTS = [(OVERLAY["host_id"], True), ("other-host", True)]
SCHEDULE = load_floor_fill_schedule()
NAMES = SCHEDULE.unit_environment
DECLARED_ENVIRONMENT = {
    NAMES.workspace_config_root: "/srv/example/config-root",
    NAMES.overlay_roots: "/srv/example/store-root:/srv/example/clone-root",
}


def host_row(**changes: Any) -> ModelLabHostRow:
    """The fill host's row with the cells the append command is built from (synthetic values)."""
    fields: dict[str, Any] = {
        "name": OVERLAY["host_id"],
        "target": "example-target",
        "ledger_bus_lane": "example-lane",
        "ledger_bus_python": "example-venv/bin/example-python",
    }
    return ModelLabHostRow(**{**fields, **changes})


def _registry(tmp_path: Path) -> Path:
    root = tmp_path / "registry"
    python = root / SCHEDULE.dispatch_venv / "bin" / "python"
    python.parent.mkdir(parents=True, exist_ok=True)
    python.write_text("", encoding="utf-8")
    return root


def _ok(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(list(command), 0, "", "")


def install(
    tmp_path: Path,
    rows: Sequence[ModelLabHostRow],
    *extra: str,
    root: Path | None = None,
    environment: Mapping[str, str] = DECLARED_ENVIRONMENT,
) -> tuple[int, str, str]:
    result = run_schedule(
        [
            "install",
            "--this-host",
            OVERLAY["host_id"],
            "--workspace-root",
            str(root or _registry(tmp_path)),
            *extra,
        ],
        {"floor_fill": OVERLAY, "floor_fill_live": live_section(tmp_path)},
        hosts=HOSTS,
        host_rows=rows,
        environment=environment,
        home=tmp_path,
        run=_ok,
    )
    return result.exit_code, result.stdout, result.stderr


def environment_of(rendered: str) -> dict[str, str]:
    """The ``Environment=`` assignments of the rendered service, as systemd reads them (``%%`` unescaped)."""
    found: dict[str, str] = {}
    for line in rendered.splitlines():
        if line.startswith("Environment="):
            word = shlex.split(line.removeprefix("Environment="))[0]
            name, _, value = word.partition("=")
            found[name] = value.replace("%%", "%")
    return found


def expected_append_command(root: Path, interpreter: str, lane: str) -> str:
    return (
        f"{interpreter} -m {SCHEDULE.ledger_append_entry} "
        f"--bus-lane {lane} --omnibase-path {root}"
    )


def test_live_unit_carries_the_three_variables_with_the_declared_values(
    tmp_path: Path,
) -> None:
    root = _registry(tmp_path)
    code, out, err = install(
        tmp_path, [host_row()], "--dry-run", "--profile", "live", root=root
    )
    assert (code, err) == (0, "")
    assert (
        environment_of(out).items()
        >= {
            NAMES.ledger_append_command: expected_append_command(
                root, f"{root}/example-venv/bin/example-python", "example-lane"
            ),
            **DECLARED_ENVIRONMENT,
        }.items()
    )


def test_the_values_are_the_declared_ones_not_fixed_text(tmp_path: Path) -> None:
    """Other declared cells render other values: nothing in the render is a literal."""
    root = _registry(tmp_path)
    row = host_row(
        ledger_bus_lane="other-lane", ledger_bus_python="/opt/other/bin/python"
    )
    other = {
        NAMES.workspace_config_root: "/srv/other/config-root",
        NAMES.overlay_roots: "/srv/other/only-root",
    }
    code, out, err = install(
        tmp_path, [row], "--dry-run", "--profile", "live", root=root, environment=other
    )
    assert (code, err) == (0, "")
    assert (
        environment_of(out).items()
        >= {
            NAMES.ledger_append_command: expected_append_command(
                root, "/opt/other/bin/python", "other-lane"
            ),
            **other,
        }.items()
    )


def test_installed_unit_equals_the_dry_run_and_carries_them(tmp_path: Path) -> None:
    root = _registry(tmp_path)
    _, dry, _ = install(
        tmp_path, [host_row()], "--dry-run", "--profile", "live", root=root
    )
    code, _, _ = install(tmp_path, [host_row()], "--profile", "live", root=root)
    assert code == 0
    unit_dir = tmp_path / ".config/systemd/user"
    service = (unit_dir / f"{SCHEDULE.unit_name}.service").read_text(encoding="utf-8")
    assert service in dry
    assert set(NAMES.names()) <= set(environment_of(service))


def test_the_in_memory_unit_is_unchanged(tmp_path: Path) -> None:
    code, out, _ = install(tmp_path, [], "--dry-run", environment={})
    assert code == 0
    assert not set(NAMES.names()) & set(environment_of(out))


@pytest.mark.parametrize(
    ("cell", "named"),
    [
        ({"ledger_bus_lane": None}, "ledger_bus_lane"),
        ({"ledger_bus_python": None}, "ledger_bus_python"),
    ],
)
def test_a_row_cell_not_declared_refuses_naming_it(
    cell: dict[str, Any], named: str, tmp_path: Path
) -> None:
    code, out, err = install(tmp_path, [host_row(**cell)], "--profile", "live")
    assert code == 1
    assert out == ""
    assert f"lab_hosts.hosts[{OVERLAY['host_id']}].{named}" in err
    assert not (tmp_path / ".config/systemd/user").exists()


def test_no_row_for_the_fill_host_refuses_naming_the_host(tmp_path: Path) -> None:
    code, out, err = install(
        tmp_path, [host_row(name="other-host")], "--profile", "live"
    )
    assert code == 1
    assert out == ""
    assert f"lab_hosts.hosts[{OVERLAY['host_id']}]" in err


@pytest.mark.parametrize("name", [NAMES.workspace_config_root, NAMES.overlay_roots])
def test_an_environment_value_not_declared_refuses_naming_the_variable(
    name: str, tmp_path: Path
) -> None:
    environment = {
        key: value for key, value in DECLARED_ENVIRONMENT.items() if key != name
    }
    code, out, err = install(
        tmp_path, [host_row()], "--profile", "live", environment=environment
    )
    assert code == 1
    assert out == ""
    assert name in err
    assert not (tmp_path / ".config/systemd/user").exists()


def test_a_blank_environment_value_is_not_declared(tmp_path: Path) -> None:
    environment = {**DECLARED_ENVIRONMENT, NAMES.overlay_roots: "  "}
    code, _out, err = install(
        tmp_path, [host_row()], "--profile", "live", environment=environment
    )
    assert code == 1
    assert NAMES.overlay_roots in err
