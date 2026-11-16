# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain of the overlay root resolver (OMN-24032).

The store root is named by the store-read overlay under the first bootstrap
root that holds it; the canonical clone sits beside the registry root; the
declared roots come last. An in-memory fake probe stands in for the filesystem.
"""

from __future__ import annotations

import pytest

from omnimarket.models.overlay_roots import (
    EnumOverlayRootSource,
    ModelOverlayRootsRequest,
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


def test_store_root_named_by_the_canonical_clone_comes_first() -> None:
    registry = "/opt/omni/registry"
    clone = "/opt/omni/overlays/overlays"
    second_clone = "/opt/omni/overlay-store-reader/overlays/overlays"
    store = "/srv/fixture/u/.local/state/onex/overlay-store"
    declared = "/opt/omni/plugins/overlays"
    probe = FakeProbe(
        dirs={clone, second_clone, store, declared},
        files={
            f"{clone}/node_overlay_store_read_effect/overlay.yaml": (
                "snapshot_root: ~/.local/state/onex/overlay-store\n"
            ),
        },
    )
    result = HandlerOverlayRootsResolve(probe).handle(
        ModelOverlayRootsRequest(
            registry_root=registry,
            home="/srv/fixture/u",
            declared_roots=(declared,),
        )
    )
    assert result.paths() == (store, clone, second_clone, declared)
    assert [p.source for p in result.roots] == [
        EnumOverlayRootSource.STORE,
        EnumOverlayRootSource.CANONICAL_CLONE,
        EnumOverlayRootSource.CANONICAL_CLONE,
        EnumOverlayRootSource.DECLARED,
    ]
    assert result.store_named_by == clone
    assert result.finding is None


def test_no_clone_falls_back_to_the_home_store_root() -> None:
    store = "/srv/fixture/u/.local/state/onex/overlay-store"
    probe = FakeProbe(dirs={store}, files={})
    result = HandlerOverlayRootsResolve(probe).handle(
        ModelOverlayRootsRequest(home="/srv/fixture/u")
    )
    assert result.paths() == (store,)
    assert result.roots[0].source == EnumOverlayRootSource.STORE
    assert result.store_named_by is None
    assert result.finding is None
