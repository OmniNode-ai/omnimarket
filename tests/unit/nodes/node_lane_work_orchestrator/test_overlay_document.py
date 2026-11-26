# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One overlay document, one model, the eight-tier chain at its top level (OMN-26721, plan 12.2)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.models.overlay_documents.model_lane_work_orchestrator_overlay import (
    ModelLaneWorkOrchestratorOverlay,
    load_orchestrator_overlay,
)

pytestmark = pytest.mark.unit

EXAMPLE = (
    Path(__file__).resolve().parents[4]
    / "src/omnimarket/nodes/node_lane_work_orchestrator/overlay.example.yaml"
)
TIERS = (
    "local",
    "glm-small",
    "glm-medium",
    "glm-large",
    "codex-cheapest",
    "claude-small",
    "claude-medium",
    "claude-large",
)


def test_the_example_validates_and_holds_the_eight_tiers_in_order_at_the_top_level() -> (
    None
):
    overlay = ModelLaneWorkOrchestratorOverlay.model_validate(
        yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    )
    assert tuple(tier.name for tier in overlay.help_tiers) == TIERS
    assert load_orchestrator_overlay().help_tiers == overlay.help_tiers


def test_the_model_refuses_a_reducer_help_tiers_key() -> None:
    document = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    document["reducer"]["help_tiers"] = document["help_tiers"][:3]
    with pytest.raises(ValidationError):
        ModelLaneWorkOrchestratorOverlay.model_validate(document)


def test_the_chain_is_spelled_once_in_the_overlay_file() -> None:
    assert EXAMPLE.read_text(encoding="utf-8").count("help_tiers") == 1


def test_no_gemini_tier_is_declared() -> None:
    assert "gemini" not in EXAMPLE.read_text(encoding="utf-8").lower()


def test_the_reducer_config_carries_the_chain_from_the_top_level_key() -> None:
    overlay = load_orchestrator_overlay()
    config = overlay.reducer_config()
    assert config.help_tiers == overlay.help_tiers
    assert config.repeat_bound == overlay.reducer.repeat_bound
