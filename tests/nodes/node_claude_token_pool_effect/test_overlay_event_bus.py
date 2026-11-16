# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The event_bus block of the pool overlay (OMN-24328).

The overlay accepts an ``event_bus`` block of the shared bus-binding shape; a kafka block must name a lane,
an inmemory block names none, and an unknown key is refused. ``onex run-node`` reads the same file, so the
block must load through the overlay model the rotation tick uses: the overlay refuses a key it does not know.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    load_structured_overlay,
)
from omnimarket.models.bus_binding.model_node_event_bus import (
    ModelNodeEventBus,
    ModelNodeEventBusOverlay,
)
from omnimarket.nodes.node_claude_token_pool_effect.models.model_claude_token_rotation import (
    ModelRotationOverlay,
)

pytestmark = pytest.mark.unit


def overlay_body() -> dict[str, Any]:
    return {
        "hosts": [
            {
                "name": name,
                "scheduler": "systemd-user",
                "venv": "omnimarket/.venv",
                "token_file": ".config/omni/claude-oauth-token",
            }
            for name in ("h053", "h101")
        ],
        "marks_source": {
            "host": "h053",
            "ssh_target": None,
            "mirror_path": ".cache/omni/claude-token-pool.json",
            "max_age_seconds": 600,
        },
        "events_file": "token-rotations.jsonl",
        "unit_environment": {},
        "schedule": {"interval_seconds": 300},
    }


def test_the_overlay_accepts_an_event_bus_block() -> None:
    body = overlay_body()
    body["event_bus"] = {"type": "kafka", "lane": "dev"}
    overlay = ModelRotationOverlay.model_validate(body)
    assert overlay.event_bus == ModelNodeEventBus(type="kafka", lane="dev")

    body = overlay_body()
    body["event_bus"] = {"type": "inmemory"}
    overlay = ModelRotationOverlay.model_validate(body)
    assert overlay.event_bus == ModelNodeEventBus(type="inmemory")

    overlay = ModelRotationOverlay.model_validate(overlay_body())
    assert overlay.event_bus is None


def test_the_overlay_refuses_a_kafka_event_bus_without_a_lane() -> None:
    body = overlay_body()
    body["event_bus"] = {"type": "kafka"}
    with pytest.raises(ValidationError):
        ModelRotationOverlay.model_validate(body)


def test_the_overlay_refuses_an_inmemory_event_bus_that_names_a_lane() -> None:
    body = overlay_body()
    body["event_bus"] = {"type": "inmemory", "lane": "dev"}
    with pytest.raises(ValidationError):
        ModelRotationOverlay.model_validate(body)


def test_the_overlay_refuses_an_event_bus_key_it_does_not_know() -> None:
    body = overlay_body()
    body["event_bus"] = {"type": "kafka", "lane": "dev", "broker": "x"}
    with pytest.raises(ValidationError):
        ModelRotationOverlay.model_validate(body)

    body = overlay_body()
    body["not_a_key"] = 1
    with pytest.raises(ValidationError):
        ModelRotationOverlay.model_validate(body)


def test_golden_the_overlay_file_under_the_roots_loads_with_its_event_bus(
    tmp_path: Path,
) -> None:
    body = overlay_body()
    body["event_bus"] = {"type": "kafka", "lane": "dev"}
    node_dir = tmp_path / "node_claude_token_pool_effect"
    node_dir.mkdir()
    (node_dir / "overlay.yaml").write_text(
        yaml.safe_dump(body, sort_keys=False), encoding="utf-8"
    )
    overlay = load_structured_overlay(
        "node_claude_token_pool_effect",
        ModelRotationOverlay,
        {"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)},
    )
    assert overlay.event_bus == ModelNodeEventBus(type="kafka", lane="dev")


def test_error_the_overlay_file_with_a_broken_event_bus_is_refused_by_the_loader(
    tmp_path: Path,
) -> None:
    body = overlay_body()
    body["event_bus"] = {"type": "kafka"}
    node_dir = tmp_path / "node_claude_token_pool_effect"
    node_dir.mkdir()
    (node_dir / "overlay.yaml").write_text(
        yaml.safe_dump(body, sort_keys=False), encoding="utf-8"
    )
    with pytest.raises(NodeOverlayError):
        load_structured_overlay(
            "node_claude_token_pool_effect",
            ModelRotationOverlay,
            {"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)},
        )


def test_seam_the_event_bus_field_is_the_shared_bus_binding_model() -> None:
    assert ModelRotationOverlay.model_fields["event_bus"].annotation == (
        ModelNodeEventBus | None
    )
    document = {"event_bus": {"type": "kafka", "lane": "dev"}}
    bus_overlay = ModelNodeEventBusOverlay.model_validate(document)
    body = overlay_body()
    body["event_bus"] = {"type": "kafka", "lane": "dev"}
    pool_overlay = ModelRotationOverlay.model_validate(body)
    assert bus_overlay.event_bus == pool_overlay.event_bus
