# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A deployment's ladder is found through its overlay; the packaged file is the default."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.models.delegation.model_packaged_routing_tiers import (
    ModelPackagedRoutingTiers,
)
from omnimarket.routing.routing_tiers_path import (
    ROUTING_TIERS_OVERLAY_NODE,
    ROUTING_TIERS_PACKAGED_DEFAULT_PATH,
    resolve_routing_tiers_path,
)

_LADDER = {
    "tiers": [
        {
            "name": "local",
            "models": [{"id": "fixture-model", "backend_id": "local-coder"}],
        }
    ]
}


@pytest.fixture(autouse=True)
def _unbound(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DELEGATION_ROUTING_TIERS_PATH", raising=False)
    monkeypatch.delenv("ONEX_SKILL_OVERLAY_ROOTS", raising=False)


def _overlay_root(tmp_path: Path) -> Path:
    path = tmp_path / "roots" / ROUTING_TIERS_OVERLAY_NODE / "overlay.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(yaml.safe_dump(_LADDER), encoding="utf-8")
    return tmp_path / "roots"


def test_the_packaged_default_is_the_fallback_and_is_a_valid_ladder() -> None:
    assert resolve_routing_tiers_path() == ROUTING_TIERS_PACKAGED_DEFAULT_PATH
    parsed = ModelPackagedRoutingTiers.model_validate(
        yaml.safe_load(ROUTING_TIERS_PACKAGED_DEFAULT_PATH.read_text(encoding="utf-8"))
    )
    assert parsed.tiers[0].name == "local"


def test_the_overlay_ladder_is_used_when_nothing_pins_a_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _overlay_root(tmp_path)
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(root))
    resolved = resolve_routing_tiers_path()
    assert resolved == root / ROUTING_TIERS_OVERLAY_NODE / "overlay.yaml"
    assert yaml.safe_load(resolved.read_text(encoding="utf-8")) == _LADDER


def test_a_pinned_file_wins_over_the_overlay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(_overlay_root(tmp_path)))
    pinned = tmp_path / "pinned.yaml"
    pinned.write_text(yaml.safe_dump(_LADDER), encoding="utf-8")
    monkeypatch.setenv("DELEGATION_ROUTING_TIERS_PATH", str(pinned))
    assert resolve_routing_tiers_path() == pinned


def test_a_root_without_the_node_overlay_leaves_the_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
    assert resolve_routing_tiers_path() == ROUTING_TIERS_PACKAGED_DEFAULT_PATH
