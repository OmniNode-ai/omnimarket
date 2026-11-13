# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for resolved overlay roots in node_overlay_reader (OMN-24032)."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest
from pydantic import BaseModel

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayNotFoundError,
    find_node_overlay,
    load_structured_overlay,
)


class _EmptyModel(BaseModel):
    pass


def _make_overlay(root: Path, node: str, content: str = "{}\n") -> Path:
    node_dir = root / node
    node_dir.mkdir(parents=True, exist_ok=True)
    overlay_file = node_dir / "overlay.yaml"
    overlay_file.write_text(content, encoding="utf-8")
    return overlay_file


def test_golden_registry_root_sibling_overlays(tmp_path: Path) -> None:
    registry_root = tmp_path / "registry"
    registry_root.mkdir()
    overlays_root = tmp_path / "overlays" / "overlays"
    _make_overlay(overlays_root, "node_a", "key: value\n")
    environ: Mapping[str, str] = {"OMNI_HOME": str(registry_root)}
    found = find_node_overlay("node_a", environ)
    assert found is not None
    assert found == overlays_root / "node_a" / "overlay.yaml"


def test_store_root_first(tmp_path: Path) -> None:
    home = tmp_path / "home"
    store_root = home / ".local" / "state" / "onex" / "overlay-store"
    _make_overlay(store_root, "node_b", "key: store_value\n")
    declared_root = tmp_path / "declared"
    _make_overlay(declared_root, "node_b", "key: declared_value\n")
    environ: Mapping[str, str] = {
        "HOME": str(home),
        "ONEX_SKILL_OVERLAY_ROOTS": str(declared_root),
    }
    found = find_node_overlay("node_b", environ)
    assert found is not None
    assert found == store_root / "node_b" / "overlay.yaml"


def test_error_chain_nothing_anywhere(tmp_path: Path) -> None:
    environ: Mapping[str, str] = {"HOME": str(tmp_path)}
    found = find_node_overlay("node_missing", environ)
    assert found is None
    with pytest.raises(NodeOverlayNotFoundError):
        load_structured_overlay("node_missing", _EmptyModel, environ)


def test_failing_control_retired_path_never_returned(tmp_path: Path) -> None:
    registry_root = tmp_path / "registry"
    retired_root = registry_root / "overlays" / "overlays"
    _make_overlay(retired_root, "node_c", "key: retired_value\n")
    environ: Mapping[str, str] = {"OMNI_HOME": str(registry_root)}
    found = find_node_overlay("node_c", environ)
    assert found is None
