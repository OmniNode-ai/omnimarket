# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A registry unit's overlay root never pins a plugin version (OMN-22809).

The overlay reader takes the first root whose ``<root>/<node>/overlay.yaml`` exists and reads that whole file: no key
falls through to a later root. A root list that ends in a version-pinned plugin cache folder
(``plugins/cache/<marketplace>/<plugin>/<x.y.z>/overlays``) keeps an old copy of a node's overlay alive after the
plugin moved on, and that copy answers the moment the first root lacks the file. The unit's declared roots carry the
plugin through ``{marketplace_root}`` only; the installer refuses a version-pinned plugin cache path in an
overlay-root variable, whether it comes from the unit's default or from a request override.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from omnimarket.handlers.rules_unit_roots import (
    UnitRootPinnedError,
    pinned_plugin_cache_version,
)
from omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units import (
    HandlerRegistryUnits,
    load_units,
)
from omnimarket.nodes.node_host_installer_registry_units.models.model_registry_units import (
    ModelRegistryUnitsRequest,
)

pytestmark = pytest.mark.unit

ROOTS = "ONEX_SKILL_OVERLAY_ROOTS"
UNIT = "job-log-filler"
PINNED = (
    "/Users/op/.claude/plugins/cache/omninode-internal/onex-overlays/0.1.30/overlays"
)


def environment_of(
    tmp_path: Path, unit: str = UNIT, units: dict[str, Any] | None = None, **fields: Any
) -> dict[str, str]:
    base: dict[str, Any] = {
        "unit": unit,
        "home": str(tmp_path / "home"),
        "registry_root": str(tmp_path / "omni"),
        "workspace_config_root": str(tmp_path / "ws"),
        "plans_path": str(tmp_path / "plans"),
        "marketplace_root": str(tmp_path / "market"),
        "runtime_ssh_host": "lab-runtime",
        "ledger_host_name": "lab-ledger-host",
    }
    declared = units or load_units()
    overrides = dict(fields.pop("path_overrides", {}))
    if "LAB_WORK_HOST_NAME" in declared[unit].get("environment_path_overrides", []):
        overrides.setdefault("LAB_WORK_HOST_NAME", "lab-host")
    request = ModelRegistryUnitsRequest(
        **{**base, **fields, "path_overrides": overrides}
    )
    handler = HandlerRegistryUnits(
        lambda argv: subprocess.CompletedProcess(argv, 0, "test-user", "")
    )
    return handler.environment(request, declared[unit])


def test_golden_the_job_log_filler_default_names_the_overlays_clone_and_the_unpinned_plugin(
    tmp_path: Path,
) -> None:
    roots = environment_of(tmp_path)[ROOTS].split(":")
    assert roots == [
        f"{tmp_path}/omni/overlays/overlays",
        f"{tmp_path}/market/plugins/onex-overlays/overlays",
    ]
    assert all(pinned_plugin_cache_version(entry) is None for entry in roots)


def test_golden_an_override_naming_only_the_overlays_clone_is_accepted(
    tmp_path: Path,
) -> None:
    clone = f"{tmp_path}/omni/overlays/overlays"
    assert environment_of(tmp_path, path_overrides={ROOTS: clone})[ROOTS] == clone


def test_golden_a_plugin_cache_path_without_a_version_segment_is_accepted(
    tmp_path: Path,
) -> None:
    current = "/Users/op/.claude/plugins/cache/omninode-internal/onex-overlays/current/overlays"
    assert environment_of(tmp_path, path_overrides={ROOTS: current})[ROOTS] == current


def test_error_an_override_pinning_a_plugin_version_is_refused_with_the_path_named(
    tmp_path: Path,
) -> None:
    with pytest.raises(UnitRootPinnedError) as refused:
        environment_of(tmp_path, path_overrides={ROOTS: PINNED})
    assert refused.value.paths == (PINNED,)
    assert refused.value.unit == UNIT
    assert "0.1.30" in str(refused.value)


def test_error_one_pinned_entry_in_a_colon_list_refuses_the_whole_list(
    tmp_path: Path,
) -> None:
    clone = f"{tmp_path}/omni/overlays/overlays"
    with pytest.raises(UnitRootPinnedError) as refused:
        environment_of(tmp_path, path_overrides={ROOTS: f"{clone}:{PINNED}"})
    assert refused.value.paths == (PINNED,)


def test_error_a_marketplace_root_that_pins_a_version_makes_the_default_refused(
    tmp_path: Path,
) -> None:
    """The default is checked as well as the override: the plugin root the install names may not be a pinned cache folder."""
    pinned_market = str(
        tmp_path / ".claude/plugins/cache/omninode-internal/onex-overlays/0.1.30"
    )
    with pytest.raises(UnitRootPinnedError):
        environment_of(tmp_path, marketplace_root=pinned_market)


@pytest.mark.parametrize(
    ("path", "version"),
    [
        (PINNED, "0.1.30"),
        ("/h/.claude/plugins/cache/m/p/10.20.300/overlays", "10.20.300"),
        ("/h/.claude/plugins/cache/m/p/0.1.40", "0.1.40"),
        ("/h/.claude/plugins/cache/m/p/current/overlays", None),
        ("/h/.claude/plugins/marketplaces/m/plugins/p/overlays", None),
        ("/h/code/overlays/overlays", None),
        ("/h/code/cache/0.1.30/overlays", None),
    ],
)
def test_pinned_plugin_cache_version_reads_only_a_versioned_plugin_cache_folder(
    path: str, version: str | None
) -> None:
    assert pinned_plugin_cache_version(path) == version


def test_every_unit_that_refuses_worktree_overlay_roots_also_refuses_a_pinned_plugin_version(
    tmp_path: Path,
) -> None:
    """No unit that holds overlay roots carries a default that pins a plugin version."""
    units = load_units()
    checked = 0
    for name, spec in units.items():
        if ROOTS not in (spec.get("environment_refuse_worktree_paths") or []):
            continue
        if "{overlays_root}" not in str(spec.get("environment", {}).get(ROOTS, "")):
            continue
        checked += 1
        with pytest.raises(UnitRootPinnedError):
            environment_of(
                tmp_path,
                name,
                units,
                ledger_path=str(tmp_path / "ledger.md"),
                path_overrides={ROOTS: PINNED},
            )
    assert checked >= 5


def test_named_break_without_the_declaration_the_pinned_override_is_accepted(
    tmp_path: Path,
) -> None:
    """The failing control: the refusal is the unit's declaration, not an accident of the path."""
    units = load_units()
    units[UNIT].pop("environment_refuse_worktree_paths")
    assert (
        environment_of(tmp_path, UNIT, units, path_overrides={ROOTS: PINNED})[ROOTS]
        == PINNED
    )
