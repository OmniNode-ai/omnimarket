# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Failing controls of the overlay root resolver (OMN-24032).

These would fail if the rule were broken: the retired inside path is never a
store or canonical clone; the declared root precedes nothing; the contract's
config keys are present and no clone relpath is the registry's own inside path.
"""

from __future__ import annotations

from importlib import resources
from pathlib import Path

import pytest
import yaml

from omnimarket.models.overlay_roots import (
    EnumOverlayRootSource,
    ModelOverlayRootsRequest,
)
from omnimarket.nodes.node_overlay_roots_resolve_effect.adapters.filesystem_probe import (
    FileSystemOverlayRootsProbe,
)
from omnimarket.nodes.node_overlay_roots_resolve_effect.handlers.handler_overlay_roots_resolve import (
    HandlerOverlayRootsResolve,
)

pytestmark = pytest.mark.unit


class FakeProbe:
    def __init__(self, dirs: set[str], files: dict[str, str]) -> None:
        self._dirs = dirs
        self._files = files

    def is_dir(self, path: str) -> bool:
        return path in self._dirs

    def read_text(self, path: str) -> str | None:
        return self._files.get(path)

    def resolve(self, path: str) -> str:
        return path


def test_the_retired_inside_path_is_never_a_store_or_clone() -> None:
    registry = "/opt/omni/registry"
    inside = f"{registry}/overlays/overlays"
    clone = "/opt/omni/overlays/overlays"
    probe = FakeProbe(
        dirs={clone, inside},
        files={
            f"{clone}/node_overlay_store_read_effect/overlay.yaml": "snapshot_root: /x\n"
        },
    )
    result = HandlerOverlayRootsResolve(probe).handle(
        ModelOverlayRootsRequest(registry_root=registry, home="/srv/fixture/u")
    )
    for probe_ in result.roots:
        if probe_.path == inside:
            assert probe_.source == EnumOverlayRootSource.DECLARED
    assert all(
        p.source != EnumOverlayRootSource.STORE
        for p in result.roots
        if p.path == inside
    )
    assert all(
        p.source != EnumOverlayRootSource.CANONICAL_CLONE
        for p in result.roots
        if p.path == inside
    )


def test_a_declared_inside_root_is_last_and_only_declared() -> None:
    registry = "/opt/omni/registry"
    inside = f"{registry}/overlays/overlays"
    clone = "/opt/omni/overlays/overlays"
    probe = FakeProbe(
        dirs={clone, inside},
        files={
            f"{clone}/node_overlay_store_read_effect/overlay.yaml": "snapshot_root: /x\n"
        },
    )
    result = HandlerOverlayRootsResolve(probe).handle(
        ModelOverlayRootsRequest(
            registry_root=registry, home="/srv/fixture/u", declared_roots=(inside,)
        )
    )
    assert result.paths()[-1] == inside
    assert result.roots[-1].source == EnumOverlayRootSource.DECLARED


def test_the_exact_order_of_the_paths_is_fixed() -> None:
    registry = "/opt/omni/registry"
    clone = "/opt/omni/overlays/overlays"
    store = "/x/store"
    declared = "/opt/omni/plugins"
    probe = FakeProbe(
        dirs={clone, store, declared},
        files={
            f"{clone}/node_overlay_store_read_effect/overlay.yaml": f"snapshot_root: {store}\n"
        },
    )
    result = HandlerOverlayRootsResolve(probe).handle(
        ModelOverlayRootsRequest(
            registry_root=registry, home="/srv/fixture/u", declared_roots=(declared,)
        )
    )
    assert result.paths() == (store, clone, declared)


def test_the_contract_declares_the_five_config_keys() -> None:
    text = (
        resources.files("omnimarket.nodes.node_overlay_roots_resolve_effect")
        .joinpath("contract.yaml")
        .read_text(encoding="utf-8")
    )
    config = yaml.safe_load(text)["config"]
    assert set(config) == {
        "overlay_file_name",
        "store_read_overlay_id",
        "store_snapshot_key",
        "store_home_relpath",
        "canonical_clone_relpaths",
    }
    # The retired inside path <registry root>/overlays/overlays is never a
    # canonical clone relpath: none of the relpaths is the registry root's own
    # name segment joined with overlays/overlays.
    assert not any(
        relpath == "registry/overlays/overlays"
        for relpath in config["canonical_clone_relpaths"]
    )


def test_the_real_probe_resolves_over_tmp_path(tmp_path: Path) -> None:
    registry = tmp_path / "registry"
    clone = tmp_path / "overlays" / "overlays"
    store = tmp_path / "home" / ".local" / "state" / "onex" / "overlay-store"
    for directory in (registry, clone, store):
        directory.mkdir(parents=True)
    (clone / "node_overlay_store_read_effect").mkdir()
    (clone / "node_overlay_store_read_effect" / "overlay.yaml").write_text(
        f"snapshot_root: {store}\n", encoding="utf-8"
    )
    probe = FileSystemOverlayRootsProbe()
    result = HandlerOverlayRootsResolve(probe).handle(
        ModelOverlayRootsRequest(
            registry_root=str(registry), home=str(tmp_path / "home")
        )
    )
    assert result.paths() == (str(store), str(clone))
