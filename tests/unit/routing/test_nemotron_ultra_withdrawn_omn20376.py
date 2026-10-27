# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20376: Nemotron Ultra is withdrawn from ordinary delegation routing.

omnimarket#4412 withdrew Nemotron Super. Ultra stayed named in the free coder
tier, so the router still carried it as a rung (it is parked behind
``explicit_pin_only``, which only hides it at selection time). This test reads
``routing_tiers.yaml`` and the committed bifrost contract and fails while any
tier, any task override or any placement names the Ultra backend. The backend
declaration stays: it is the default judge (``adapter_routing_resolved_judge``)
and an explicit pin can still reach it.

The north-mini-code entry that served the Ultra rung's classes through a
mirrored placement now owns its use_for, written out in the tiers file.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.adapters.llm.bifrost.config_loader_bifrost_delegation import (
    load_bifrost_backend_placements,
)
from omnimarket.models.delegation.wire import parse_delegation_config_yaml
from omnimarket.routing.backend_placement import apply_backend_placements
from tests.placed_routing_config import placed_routing_config

pytestmark = pytest.mark.unit

_CONFIGS = Path(__file__).resolve().parents[3] / "src" / "omnimarket" / "configs"
_ULTRA = "openrouter-nemotron-ultra"
_NORTH_MINI = "openrouter-north-mini-code"
_CEILING_TIER = "claude"

# The classes the mirrored north-mini entry served before the withdrawal: the
# Ultra rung's use_for, minus nothing (the placement carried no narrowing).
_NORTH_MINI_SERVES = (
    "code_generation",
    "reasoning",
    "review",
    "research",
    "refactor",
    "test",
    "validator_generation",
    "document",
    "documentation",
    "summarization",
)


def _walk(node: Any) -> list[str]:
    """Every string value and mapping key under ``node``."""
    if isinstance(node, str):
        return [node]
    if isinstance(node, dict):
        found: list[str] = []
        for key, value in node.items():
            found.extend(_walk(key))
            found.extend(_walk(value))
        return found
    if isinstance(node, list):
        return [text for item in node for text in _walk(item)]
    return []


def _tiers_text() -> str:
    return (_CONFIGS / "routing_tiers.yaml").read_text(encoding="utf-8")


def test_no_ordinary_rung_or_ceiling_fallback_names_ultra() -> None:
    config = parse_delegation_config_yaml(_tiers_text())
    named = [
        (tier.name, model.backend_ref)
        for tier in config.tiers
        for model in tier.models
        if model.backend_ref == _ULTRA
    ]
    assert named == [], f"Ultra is still a tier rung: {named}"


def test_no_value_anywhere_in_the_tiers_file_names_ultra() -> None:
    values = _walk(yaml.safe_load(_tiers_text()))
    assert [v for v in values if "nemotron" in v.lower()] == []


def test_the_ceiling_tier_names_no_ultra_fallback() -> None:
    config = parse_delegation_config_yaml(_tiers_text())
    ceiling = next(tier for tier in config.tiers if tier.name == _CEILING_TIER)
    assert [model.backend_ref for model in ceiling.models] == ["cloud-gemini-2-5-flash"]


def test_no_placement_mirrors_ultra() -> None:
    placed = load_bifrost_backend_placements(
        config_path=_CONFIGS / "bifrost_delegation.yaml"
    )
    assert [b.backend_id for b in placed if _ULTRA in b.placement.fallback_for] == []


def test_the_placed_ladder_carries_no_ultra_rung() -> None:
    config = parse_delegation_config_yaml(_tiers_text())
    placed = load_bifrost_backend_placements(
        config_path=_CONFIGS / "bifrost_delegation.yaml"
    )
    ladder = apply_backend_placements(config, placed)
    assert _ULTRA not in {m.backend_ref for t in ladder.tiers for m in t.models}


def test_the_tiers_file_names_no_vendor_backend_in_the_free_tier() -> None:
    config = parse_delegation_config_yaml(_tiers_text())
    free = next(tier for tier in config.tiers if tier.name == "cheap_frontier")
    assert free.models == ()


def test_north_mini_owns_its_use_for_in_the_placed_free_tier() -> None:
    free = next(
        tier for tier in placed_routing_config().tiers if tier.name == "cheap_frontier"
    )
    assert [m.backend_ref for m in free.models] == [_NORTH_MINI]
    (entry,) = free.models
    assert entry.use_for == _NORTH_MINI_SERVES
    assert entry.id == "cohere/north-mini-code:free"
    assert entry.max_context_tokens == 256000
    assert entry.fast_path_threshold_tokens == 8192


def test_the_placement_writes_its_use_for_out_and_mirrors_no_rung() -> None:
    placed = load_bifrost_backend_placements(
        config_path=_CONFIGS / "bifrost_delegation.yaml"
    )
    (north_mini,) = [b for b in placed if b.backend_id == _NORTH_MINI]
    assert north_mini.placement.fallback_for == ()
    assert north_mini.placement.use_for == _NORTH_MINI_SERVES
    assert "code_review" not in _NORTH_MINI_SERVES


def test_ultra_backend_stays_declared_pin_only_for_the_judge() -> None:
    contract = yaml.safe_load(
        (_CONFIGS / "bifrost_delegation.yaml").read_text(encoding="utf-8")
    )
    ultra = next(b for b in contract["backends"] if b["backend_id"] == _ULTRA)
    assert ultra["explicit_pin_only"] is True
    assert "judge_adequacy" in ultra["capabilities"]
