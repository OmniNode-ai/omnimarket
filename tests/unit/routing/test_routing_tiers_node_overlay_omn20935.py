# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The tier ladder resolves pin, then node overlay, then the packaged file (OMN-20935)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.inference.delegation_config_provenance import (
    EnumDelegationConfigSource,
    resolve_path_config,
)
from omnimarket.routing.routing_tiers_path import (
    ROUTING_TIERS_OVERLAY_NODE,
    ROUTING_TIERS_PACKAGED_DEFAULT_PATH,
    ROUTING_TIERS_PATH_ENV_KEY,
    resolve_routing_tiers_path,
)
from tests.node_overlay_support import install_node_overlay

LADDER = {"tiers": [{"name": "local", "models": []}]}


@pytest.fixture(autouse=True)
def _unpinned(monkeypatch: pytest.MonkeyPatch) -> None:
    """The suite's autouse pin is removed: these tests exercise the resolution itself."""
    monkeypatch.delenv(ROUTING_TIERS_PATH_ENV_KEY, raising=False)
    monkeypatch.delenv("ONEX_SKILL_OVERLAY_ROOTS", raising=False)


@pytest.mark.unit
def test_with_nothing_supplied_the_packaged_file_is_the_ladder() -> None:
    assert resolve_routing_tiers_path() == ROUTING_TIERS_PACKAGED_DEFAULT_PATH


@pytest.mark.unit
def test_the_node_overlay_replaces_the_packaged_ladder(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    overlay = install_node_overlay(
        monkeypatch, tmp_path, ROUTING_TIERS_OVERLAY_NODE, LADDER
    )
    assert resolve_routing_tiers_path() == overlay
    assert yaml.safe_load(overlay.read_text(encoding="utf-8")) == LADDER


@pytest.mark.unit
def test_the_pin_wins_over_the_node_overlay(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    install_node_overlay(monkeypatch, tmp_path, ROUTING_TIERS_OVERLAY_NODE, LADDER)
    pinned = tmp_path / "pinned.yaml"
    pinned.write_text("tiers: []\n", encoding="utf-8")
    monkeypatch.setenv(ROUTING_TIERS_PATH_ENV_KEY, str(pinned))
    assert resolve_routing_tiers_path() == pinned


@pytest.mark.unit
def test_provenance_names_the_node_overlay_as_the_source(tmp_path: Path) -> None:
    overlay = tmp_path / "overlay.yaml"
    resolved, provenance = resolve_path_config(
        ROUTING_TIERS_PATH_ENV_KEY, tmp_path / "packaged.yaml", node_overlay=overlay
    )
    assert resolved == overlay
    assert provenance.source is EnumDelegationConfigSource.NODE_OVERLAY
    assert provenance.override_present is False
    assert "source=node_overlay" in provenance.log_line()
