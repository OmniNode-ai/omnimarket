# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19998: the verdict thresholds come from the overlay, never from code defaults."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

import omnimarket.nodes.node_pool_usage_verdict_compute as node_package
from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_pool_usage_verdict_compute.models.model_pool_usage_policy import (
    ModelPoolUsagePolicy,
    ModelPoolUsageWindowPolicy,
)
from omnimarket.nodes.node_pool_usage_verdict_compute.overlay import (
    NODE_NAME,
    load_policy,
)

pytestmark = pytest.mark.unit

NODE_DIR = Path(node_package.__file__).parent


def _write_overlay(tmp_path: Path, document: object) -> None:
    root = tmp_path / NODE_NAME
    root.mkdir()
    (root / "overlay.yaml").write_text(yaml.safe_dump(document), encoding="utf-8")


def test_load_policy_yields_the_example_values(tmp_path: Path) -> None:
    document = yaml.safe_load((NODE_DIR / "overlay.example.yaml").read_text())
    _write_overlay(tmp_path, document)

    policy = load_policy({"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)})

    assert policy == ModelPoolUsagePolicy(
        five_hour=ModelPoolUsageWindowPolicy(drain_at=0.9, limited_at=1.0),
        weekly=ModelPoolUsageWindowPolicy(drain_at=0.85, limited_at=1.0),
        max_reading_age_seconds=900,
    )


def test_an_overlay_with_an_unknown_key_is_refused(tmp_path: Path) -> None:
    document = yaml.safe_load((NODE_DIR / "overlay.example.yaml").read_text())
    document["bonus_threshold"] = 0.5
    _write_overlay(tmp_path, document)

    with pytest.raises(NodeOverlayError) as raised:
        load_policy({"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)})

    assert "bonus_threshold" in str(raised.value)


def test_an_overlay_missing_max_reading_age_seconds_is_refused(tmp_path: Path) -> None:
    document = yaml.safe_load((NODE_DIR / "overlay.example.yaml").read_text())
    del document["max_reading_age_seconds"]
    _write_overlay(tmp_path, document)

    with pytest.raises(NodeOverlayError) as raised:
        load_policy({"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)})

    assert "max_reading_age_seconds" in str(raised.value)
