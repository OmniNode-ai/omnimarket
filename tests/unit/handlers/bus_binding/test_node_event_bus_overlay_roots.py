# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-24032: the node bus overlay lookup uses the one overlay root resolver.

The overlay is read store root first, then the canonical overlays clone beside the registry root,
then the declared roots, and last the frozen config root. A node whose overlay lives in the
overlays repository is found, and a frozen config root overlay still resolves when no overlays
root holds the file.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.delegated_test_loop.lane_bus import LabRunBusError
from omnimarket.handlers.bus_binding.node_event_bus_overlay import (
    resolve_node_event_bus,
)

pytestmark = pytest.mark.unit

_KAFKA = "event_bus:\n  type: kafka\n  lane: dev\n"
_INMEMORY = "event_bus:\n  type: inmemory\n"


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_the_canonical_clone_overlay_is_found(tmp_path: Path) -> None:
    """A node whose overlay lives in the overlays repository is found through the resolver."""
    registry_root = tmp_path / "registry"
    registry_root.mkdir()
    clone_file = _write(
        tmp_path / "overlays" / "overlays" / "node_demo_effect" / "overlay.yaml", _KAFKA
    )
    environ = {"HOME": str(tmp_path / "home")}
    bus, path = resolve_node_event_bus("node_demo_effect", registry_root, environ)
    assert bus is not None
    assert (bus.type, bus.lane) == ("kafka", "dev")
    assert path == clone_file


def test_a_declared_root_overlay_is_found(tmp_path: Path) -> None:
    """Only a declared root holds the overlay, and it is found."""
    registry_root = tmp_path / "registry"
    registry_root.mkdir()
    declared_root = tmp_path / "declared"
    declared_file = _write(
        declared_root / "node_demo_effect" / "overlay.yaml", _INMEMORY
    )
    environ = {
        "HOME": str(tmp_path / "home"),
        "ONEX_SKILL_OVERLAY_ROOTS": str(declared_root),
    }
    bus, path = resolve_node_event_bus("node_demo_effect", registry_root, environ)
    assert bus is not None
    assert bus.type == "inmemory"
    assert path == declared_file


def test_the_store_root_wins_over_the_clone(tmp_path: Path) -> None:
    """The store root's overlay wins; with the store file removed the clone's is read."""
    registry_root = tmp_path / "registry"
    registry_root.mkdir()
    store_file = _write(
        tmp_path
        / "home"
        / ".local"
        / "state"
        / "onex"
        / "overlay-store"
        / "node_demo_effect"
        / "overlay.yaml",
        _KAFKA,
    )
    clone_file = _write(
        tmp_path / "overlays" / "overlays" / "node_demo_effect" / "overlay.yaml",
        _INMEMORY,
    )
    environ = {"HOME": str(tmp_path / "home")}
    bus, path = resolve_node_event_bus("node_demo_effect", registry_root, environ)
    assert bus is not None
    assert bus.type == "kafka"
    assert path == store_file

    store_file.unlink()
    bus, path = resolve_node_event_bus("node_demo_effect", registry_root, environ)
    assert bus is not None
    assert bus.type == "inmemory"
    assert path == clone_file


def test_the_frozen_config_root_resolves_last(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the frozen config root holds the overlay, and it still resolves."""
    config_root = tmp_path / "config-owner"
    config_file = _write(
        config_root
        / "config"
        / "onex"
        / "overlays"
        / "node_demo_effect"
        / "overlay.yaml",
        _KAFKA,
    )
    monkeypatch.setenv("ONEX_WORKSPACE_CONFIG_ROOT", str(config_root))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    environ = {"HOME": str(tmp_path / "home")}
    bus, path = resolve_node_event_bus("node_demo_effect", workspace, environ)
    assert bus is not None
    assert (bus.type, bus.lane) == ("kafka", "dev")
    assert path == config_file


def test_a_malformed_event_bus_raises_naming_the_file(tmp_path: Path) -> None:
    """A present but malformed ``event_bus`` raises, naming the file and the fields."""
    registry_root = tmp_path / "registry"
    registry_root.mkdir()
    clone_file = _write(
        tmp_path / "overlays" / "overlays" / "node_demo_effect" / "overlay.yaml",
        "event_bus:\n  type: kafka\n",
    )
    environ = {"HOME": str(tmp_path / "home")}
    with pytest.raises(LabRunBusError) as excinfo:
        resolve_node_event_bus("node_demo_effect", registry_root, environ)
    message = str(excinfo.value)
    assert str(clone_file) in message
    assert "lane" in message


def test_an_overlay_without_event_bus_answers_none(tmp_path: Path) -> None:
    """An overlay with no ``event_bus`` key answers (None, path)."""
    registry_root = tmp_path / "registry"
    registry_root.mkdir()
    clone_file = _write(
        tmp_path / "overlays" / "overlays" / "node_demo_effect" / "overlay.yaml",
        "contract: {}\n",
    )
    environ = {"HOME": str(tmp_path / "home")}
    bus, path = resolve_node_event_bus("node_demo_effect", registry_root, environ)
    assert bus is None
    assert path == clone_file


def test_no_overlay_anywhere_answers_none(tmp_path: Path) -> None:
    """No overlay anywhere and no config root answers (None, None)."""
    environ = {"HOME": str(tmp_path / "home")}
    bus, path = resolve_node_event_bus("node_demo_effect", None, environ)
    assert bus is None
    assert path is None


def test_the_retired_path_is_not_found(tmp_path: Path) -> None:
    """A file at the retired <registry root>/overlays/overlays path is not found."""
    registry_root = tmp_path / "registry"
    registry_root.mkdir()
    _write(
        registry_root / "overlays" / "overlays" / "node_demo_effect" / "overlay.yaml",
        _KAFKA,
    )
    environ = {"HOME": str(tmp_path / "home")}
    bus, _path = resolve_node_event_bus("node_demo_effect", registry_root, environ)
    assert bus is None
