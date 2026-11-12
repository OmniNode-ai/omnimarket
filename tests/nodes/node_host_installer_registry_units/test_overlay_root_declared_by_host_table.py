# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_host_installer_registry_units: {overlays_root} is the clone the host's lab_hosts row declares.

The installer's {overlays_root} placeholder is filled from the host table: the overlays directory of
the overlays clone (repo OmniNode-ai/overlays at ref main) that the host's own row declares, with
$OMNI_HOME expanded against the request's registry root and the result normalised. A row that
declares no overlays clone, a host the table does not know, a request that names no host and a
table that cannot be read all refuse the install with the host (or the missing input) named; a
path override wins without the table being read, and a declared root under omni_worktrees is
still refused.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.handlers.rules_unit_roots import UnitRootRefusedError
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
    ModelRegistryUnitsResult,
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
FLOOR_UNIT = "factory-floor-publish"
SWEEP_UNIT = "lane-closeout-sweep-systemd"
LINUX_PYTHON = "/usr/bin/python3.12"
OVERLAYS_REPO = "OmniNode-ai/overlays"


def table(*rows: ModelLabHostRow) -> ModelLabHostsTable:
    """The smallest valid lab host table: the rows given, and nothing else."""
    return ModelLabHostsTable(hosts=rows)


def row(name: str, **clones: ModelLabHostClone) -> ModelLabHostRow:
    """One host row: its name, a lab target and the clones it declares."""
    return ModelLabHostRow(
        name=name, target=f"{name}.lab", clones=tuple(clones.values())
    )


def overlays_clone(path: str) -> ModelLabHostClone:
    """The overlays clone at ``path``, at the repo and ref the host table declares for it."""
    return ModelLabHostClone(repo=OVERLAYS_REPO, ref="main", path=path)


def handler(rows: tuple[ModelLabHostRow, ...]) -> HandlerRegistryUnits:
    """The installer whose host table is the rows: the loader never reads the overlay store."""
    return HandlerRegistryUnits(Recorder(), hosts=lambda: table(*rows))


def render_floor(
    tmp_path: Path,
    registry_root: Path,
    host: str,
    rows: tuple[ModelLabHostRow, ...],
    **fields: Any,
) -> ModelRegistryUnitsResult:
    make_checkout(registry_root / "omnimarket")
    return handler(rows).handle(
        request(
            tmp_path,
            unit=FLOOR_UNIT,
            mode="dry-run",
            registry_root=str(registry_root),
            host_name=host,
            **fields,
        )
    )


def render_sweep(
    tmp_path: Path,
    host: str,
    rows: tuple[ModelLabHostRow, ...],
    **fields: Any,
) -> ModelRegistryUnitsResult:
    make_checkout(tmp_path / "omni" / "omnimarket")
    return handler(rows).handle(
        request(
            tmp_path,
            unit=SWEEP_UNIT,
            mode="dry-run",
            host_name=host,
            path_overrides={"LAB_WORK_HOST_NAME": "h101"},
            **fields,
        )
    )


@pytest.fixture(autouse=True)
def brew_python(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units.interpreter_present",
        lambda path: path in (BREW_PYTHON, LINUX_PYTHON),
    )
    monkeypatch.setattr(
        shutil, "which", lambda name: "/usr/bin/uv" if name == "uv" else None
    )


def test_golden_mac_layout_lists_the_clone_beside_the_registry_root_first(
    tmp_path: Path,
) -> None:
    """Failing control: on the base code the first root is the path under the registry root,
    <tmp_path>/registry/overlays/overlays, which the old contract declared; the clone the row
    declares beside the registry root (normalised, no '..') comes first."""
    result = render_floor(
        tmp_path,
        tmp_path / "registry",
        "h200",
        (row("h200", overlays=overlays_clone("$OMNI_HOME/../overlays")),),
    )
    assert result.status == "success", result.stderr
    roots = plist_of(result.rendered)["EnvironmentVariables"][ROOTS].split(":")
    assert roots == [
        f"{tmp_path}/overlays/overlays",
        f"{tmp_path}/market/plugins/onex-overlays/overlays",
    ]


def test_golden_linux_layout_keeps_the_clone_under_the_registry_root(
    tmp_path: Path,
) -> None:
    result = render_sweep(
        tmp_path,
        "h101",
        (row("h101", overlays=overlays_clone("$OMNI_HOME/overlays")),),
    )
    assert result.status == "success", result.stderr
    assert f'"{ROOTS}={tmp_path}/omni/overlays/overlays"' in result.rendered


def test_a_path_override_wins_and_the_table_is_not_read(tmp_path: Path) -> None:
    override = str(tmp_path / "x")

    def hosts() -> ModelLabHostsTable:
        raise AssertionError(
            "the override supplies the root, so the table must not be read"
        )

    make_checkout(tmp_path / "omni" / "omnimarket")
    result = HandlerRegistryUnits(Recorder(), hosts=hosts).handle(
        request(
            tmp_path,
            mode="dry-run",
            host_name="h200",
            path_overrides={ROOTS: override},
        )
    )
    assert result.status == "success", result.stderr
    assert plist_of(result.rendered)["EnvironmentVariables"][ROOTS] == override


def test_a_host_row_with_no_overlays_clone_refuses_naming_the_host(
    tmp_path: Path,
) -> None:
    result = render_floor(
        tmp_path,
        tmp_path / "omni",
        "h200",
        (
            row(
                "h200",
                omnimarket=ModelLabHostClone(
                    repo="OmniNode-ai/omnimarket",
                    ref="main",
                    path="$OMNI_HOME/omnimarket",
                ),
            ),
        ),
    )
    assert result.status == "failure"
    assert result.exit_code == 1
    assert "h200" in result.stderr
    assert "overlays clone" in result.stderr


def test_an_unknown_host_refuses_naming_it(tmp_path: Path) -> None:
    result = render_floor(
        tmp_path,
        tmp_path / "omni",
        "h999",
        (row("h200", overlays=overlays_clone("$OMNI_HOME/overlays")),),
    )
    assert result.status == "failure"
    assert result.exit_code == 1
    assert "h999" in result.stderr


def test_no_host_name_refuses_naming_the_missing_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ONEX_LEDGER_HOST_NAME", raising=False)
    result = render_floor(
        tmp_path,
        tmp_path / "omni",
        "",
        (row("h200", overlays=overlays_clone("$OMNI_HOME/overlays")),),
        ledger_host_name="",
    )
    assert result.status == "failure"
    assert result.exit_code == 1
    assert "host_name" in result.stderr


def test_an_unreadable_table_refuses(tmp_path: Path) -> None:
    def hosts() -> ModelLabHostsTable:
        raise NodeOverlayError("boom")

    make_checkout(tmp_path / "omni" / "omnimarket")
    result = HandlerRegistryUnits(Recorder(), hosts=hosts).handle(
        request(tmp_path, mode="dry-run", host_name="h200")
    )
    assert result.status == "failure"
    assert result.exit_code == 1
    assert "lab host table cannot be read" in result.stderr


def test_a_declared_root_under_omni_worktrees_is_still_refused(
    tmp_path: Path,
) -> None:
    make_checkout(tmp_path / "omni" / "omnimarket")
    unit = load_units()[FLOOR_UNIT]
    with pytest.raises(UnitRootRefusedError) as refused:
        handler(
            (
                row(
                    "h101",
                    overlays=overlays_clone("$OMNI_HOME/omni_worktrees/x/overlays"),
                ),
            )
        ).environment(request(tmp_path, mode="dry-run", host_name="h101"), unit)
    assert refused.value.paths == (
        f"{tmp_path}/omni/omni_worktrees/x/overlays/overlays",
    )


def test_every_unit_root_template_names_the_declared_root_never_the_registry_root() -> (
    None
):
    """Failing control: the base contract built every overlay root under the registry root."""
    for name, unit in load_units().items():
        for value in (unit.get("environment") or {}).values():
            assert "{registry_root}/overlays" not in str(value), name
