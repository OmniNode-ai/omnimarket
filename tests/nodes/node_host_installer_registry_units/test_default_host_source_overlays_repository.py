# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_host_installer_registry_units: the DEFAULT host table is the overlays repository's, not the config owner's copy.

Golden chain: an install on the Mac layout with no injected table reads the overlays repository's
lab_hosts overlay through the overlay roots and lists the clone its row declares beside the registry
root first, even when the shell also lists the config owner's retired copy first. Error chain: a shell
whose only table is the retired copy refuses the install, naming that the table cannot be read.
Failing control: on the base reader the first test renders from the retired copy and the second renders.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from omnimarket.handlers.node_overlay_reader import OVERLAY_ROOTS_ENV
from omnimarket.handlers.overlay_keys import CONFIG_ROOT_ENV, LAB_HOSTS_FILE_ENV
from omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units import (
    HandlerRegistryUnits,
    default_hosts,
)
from tests.nodes.node_host_installer_registry_units.test_registry_units import (
    BREW_PYTHON,
    Recorder,
    make_checkout,
    plist_of,
    request,
)

pytestmark = pytest.mark.unit

FLOOR_UNIT = "factory-floor-publish"
OVERLAYS_REPO = "OmniNode-ai/overlays"
RETIRED_PARTS = ("config", "onex", "overlays")


def write_table(root: Path, clones: list[dict[str, str]]) -> Path:
    """``<root>/lab_hosts/overlay.yaml`` with an h200 row declaring ``clones``; returns ``root``."""
    path = root / "lab_hosts" / "overlay.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(
        yaml.safe_dump(
            {
                "hosts": [{"name": "h200", "target": "192.0.2.10", "clones": clones}],
                "registry_root_candidates": ["/srv/example/registry"],
            }
        ),
        encoding="utf-8",
    )
    return root


@pytest.fixture(autouse=True)
def brew_python(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units.interpreter_present",
        lambda path: path == BREW_PYTHON,
    )
    monkeypatch.setattr(
        shutil, "which", lambda name: "/usr/bin/uv" if name == "uv" else None
    )
    monkeypatch.delenv(LAB_HOSTS_FILE_ENV, raising=False)


def install_dry_run(tmp_path: Path):
    registry = tmp_path / "registry"
    make_checkout(registry / "omnimarket")
    # The tests/nodes conftest stubs the root for the default loader itself; this wrapper reaches the real
    # default reader (default_hosts) so the resolution under test runs.
    return HandlerRegistryUnits(Recorder(), hosts=lambda: default_hosts()).handle(
        request(
            tmp_path,
            unit=FLOOR_UNIT,
            mode="dry-run",
            registry_root=str(registry),
            host_name="h200",
        )
    )


def test_golden_the_default_table_is_the_overlays_repository_s(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "config-owner"
    overlays = write_table(
        tmp_path / "overlays" / "overlays",
        [{"repo": OVERLAYS_REPO, "ref": "main", "path": "$OMNI_HOME/../overlays"}],
    )
    retired = write_table(config.joinpath(*RETIRED_PARTS), [])
    monkeypatch.setenv(CONFIG_ROOT_ENV, str(config))
    monkeypatch.setenv(OVERLAY_ROOTS_ENV, f"{retired}:{overlays}")
    result = install_dry_run(tmp_path)
    assert result.status == "success", result.stderr
    roots = plist_of(result.rendered)["EnvironmentVariables"][OVERLAY_ROOTS_ENV]
    assert roots.split(":")[0] == f"{tmp_path}/overlays/overlays"


def test_error_the_retired_copy_alone_refuses_the_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "config-owner"
    retired = write_table(config.joinpath(*RETIRED_PARTS), [])
    monkeypatch.setenv(CONFIG_ROOT_ENV, str(config))
    monkeypatch.setenv(OVERLAY_ROOTS_ENV, str(retired))
    result = install_dry_run(tmp_path)
    assert result.status == "failure"
    assert result.exit_code == 1
    assert "lab host table cannot be read" in result.stderr
