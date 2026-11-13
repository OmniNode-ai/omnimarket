# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The optional event_bus key of the GPU power caps overlay (OMN-24201): the bus onex run-node reads to reach the node.

Shape followed: test_overlay_event_bus_omn19998.py in node_work_progress_compute (a golden, an absent, three refusals,
the shipped examples and a failing control).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.handlers.bus_binding.node_event_bus_overlay import read_node_event_bus
from omnimarket.models.bus_binding.model_node_event_bus import ModelNodeEventBus
from omnimarket.nodes.node_gpu_power_caps_effect.models.model_gpu_power_caps_overlay import (
    ModelGpuPowerCapsOverlay,
)
from tests.unit.nodes.node_gpu_power_caps_effect.fixtures import NODE_DIR
from tests.unit.nodes.node_gpu_power_caps_effect.test_gpu_power_caps_lab_work import (
    overlay_document,
)

pytestmark = pytest.mark.unit


def test_event_bus_golden_validates_and_reads_back(tmp_path: Path) -> None:
    doc = overlay_document()
    doc["event_bus"] = {"type": "kafka", "lane": "dev"}
    model = ModelGpuPowerCapsOverlay.model_validate(doc)
    assert model.event_bus == ModelNodeEventBus(type="kafka", lane="dev")
    path = tmp_path / "overlay.yaml"
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    assert read_node_event_bus(path) == ModelNodeEventBus(type="kafka", lane="dev")


def test_event_bus_absent_loads_as_none() -> None:
    doc = overlay_document()
    doc.pop("event_bus", None)
    model = ModelGpuPowerCapsOverlay.model_validate(doc)
    assert model.event_bus is None
    return


def _test_event_bus_absent_loads_as_none_old() -> None:
    doc = overlay_document()
    doc.pop("event_bus", None)
    assert ModelGpuPowerCapsOverlay.model_validate(doc).event_bus is None


def test_event_bus_missing_lane_refused() -> None:
    doc = overlay_document()
    doc["event_bus"] = {"type": "kafka"}
    with pytest.raises(ValidationError):
        ModelGpuPowerCapsOverlay.model_validate(doc)


def test_event_bus_unknown_top_level_key_refused() -> None:
    doc = overlay_document()
    doc["no_such_key"] = 1
    with pytest.raises(ValidationError):
        ModelGpuPowerCapsOverlay.model_validate(doc)


def test_event_bus_unknown_inner_key_refused() -> None:
    doc = overlay_document()
    doc["event_bus"] = {"type": "kafka", "lane": "dev", "nope": 1}
    with pytest.raises(ValidationError):
        ModelGpuPowerCapsOverlay.model_validate(doc)


def test_event_bus_shipped_examples_carry_the_key() -> None:
    for name in ("lab_work.example.yaml", "lab_work_runtime.example.yaml"):
        overlay = ModelGpuPowerCapsOverlay.model_validate(
            yaml.safe_load((NODE_DIR / "overlays" / name).read_text(encoding="utf-8"))
        )
        assert overlay.event_bus is not None
        assert overlay.event_bus.type == "kafka"
        assert overlay.event_bus.lane == "example-lane"


def test_model_accepts_event_bus_key_failing_control() -> None:
    # At the merge base the model kept extra="forbid" and refused the key; the field is now declared.
    doc = overlay_document()
    doc["event_bus"] = {"type": "kafka", "lane": "dev"}
    model = ModelGpuPowerCapsOverlay.model_validate(doc)
    assert model.event_bus is not None
