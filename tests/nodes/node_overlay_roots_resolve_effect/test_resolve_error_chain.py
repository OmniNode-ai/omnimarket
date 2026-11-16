# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Error chain of the overlay root resolver (OMN-24032).

Absent candidates are named in the finding and left out of the roots; a
store-read overlay without the snapshot key falls to the home fallback; a
missing config key raises a ValueError naming the key; extra request fields
are refused by the frozen model.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from omnimarket.models.overlay_roots import (
    EnumOverlayRootSource,
    ModelOverlayRootsRequest,
)
from omnimarket.nodes.node_overlay_roots_resolve_effect.handlers import (
    handler_overlay_roots_resolve as module,
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


def test_absent_candidates_are_named_in_the_finding_and_left_out() -> None:
    registry = "/opt/omni/registry"
    clone = "/opt/omni/overlays/overlays"
    probe = FakeProbe(dirs=set(), files={})
    result = HandlerOverlayRootsResolve(probe).handle(
        ModelOverlayRootsRequest(
            registry_root=registry,
            home="/srv/fixture/u",
            declared_roots=("/opt/omni/plugins",),
        )
    )
    assert result.roots == ()
    assert result.finding is not None
    assert f"{clone} ({EnumOverlayRootSource.CANONICAL_CLONE.value})" in result.finding
    assert "/opt/omni/plugins (declared)" in result.finding
    assert "/srv/fixture/u/.local/state/onex/overlay-store (store)" in result.finding


def test_store_read_overlay_without_snapshot_key_falls_to_home() -> None:
    registry = "/opt/omni/registry"
    clone = "/opt/omni/overlays/overlays"
    store = "/srv/fixture/u/.local/state/onex/overlay-store"
    probe = FakeProbe(
        dirs={clone, store},
        files={
            f"{clone}/node_overlay_store_read_effect/overlay.yaml": "other_key: x\n"
        },
    )
    result = HandlerOverlayRootsResolve(probe).handle(
        ModelOverlayRootsRequest(registry_root=registry, home="/srv/fixture/u")
    )
    assert result.paths() == (store, clone)
    assert result.store_named_by is None


def test_a_missing_config_key_raises_value_error_naming_the_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broken = {
        k: v for k, v in module.contract_config().items() if k != "store_home_relpath"
    }
    monkeypatch.setattr(module, "contract_config", lambda: broken)
    probe = FakeProbe(dirs=set(), files={})
    with pytest.raises(ValueError, match="store_home_relpath"):
        HandlerOverlayRootsResolve(probe).handle(ModelOverlayRootsRequest())


def test_extra_request_fields_are_refused() -> None:
    with pytest.raises(ValidationError):
        ModelOverlayRootsRequest(invented="nope")
