# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The host role baseline overlay document carries the optional ``event_bus`` key (OMN-24410).

``onex run-node`` reads ``event_bus`` from the node's overlay document. The model must accept the
optional key while keeping ``extra="forbid"`` on every other key.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.handlers.bus_binding.node_event_bus_overlay import read_node_event_bus
from omnimarket.models.bus_binding.model_node_event_bus import ModelNodeEventBus
from omnimarket.nodes.node_host_role_baseline_compute.models.model_host_role_baseline import (
    ModelHostRoleBaselines,
)

pytestmark = pytest.mark.unit

_HOST_ROLE_BASELINE_DIR = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_host_role_baseline_compute"
)


def _required_document() -> dict:
    document = yaml.safe_load(
        (_HOST_ROLE_BASELINE_DIR / "overlay.example.yaml").read_text("utf-8")
    )
    document.pop("event_bus", None)
    return document


def _document(tmp_path: Path, document: dict) -> Path:
    path = tmp_path / "overlay.yaml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    return path


def test_host_role_baseline_golden_event_bus_kafka_dev(tmp_path: Path) -> None:
    document = {**_required_document(), "event_bus": {"type": "kafka", "lane": "dev"}}
    path = _document(tmp_path, document)
    model = ModelHostRoleBaselines.model_validate(
        yaml.safe_load(path.read_text("utf-8"))
    )
    assert model.event_bus == ModelNodeEventBus(type="kafka", lane="dev")
    bus = read_node_event_bus(path)
    assert bus == ModelNodeEventBus(type="kafka", lane="dev")


def test_host_role_baseline_without_event_bus_loads_with_none(tmp_path: Path) -> None:
    path = _document(tmp_path, _required_document())
    model = ModelHostRoleBaselines.model_validate(
        yaml.safe_load(path.read_text("utf-8"))
    )
    assert model.event_bus is None


def test_host_role_baseline_event_bus_kafka_without_lane_is_refused(
    tmp_path: Path,
) -> None:
    document = {**_required_document(), "event_bus": {"type": "kafka"}}
    path = _document(tmp_path, document)
    with pytest.raises(ValidationError):
        ModelHostRoleBaselines.model_validate(yaml.safe_load(path.read_text("utf-8")))


def test_host_role_baseline_unknown_top_level_key_is_still_refused(
    tmp_path: Path,
) -> None:
    document = {**_required_document(), "some_other_key": 1}
    path = _document(tmp_path, document)
    with pytest.raises(ValidationError):
        ModelHostRoleBaselines.model_validate(yaml.safe_load(path.read_text("utf-8")))


def test_host_role_baseline_unknown_inner_key_is_still_refused(tmp_path: Path) -> None:
    document = {
        **_required_document(),
        "event_bus": {"type": "kafka", "lane": "dev", "bogus": 1},
    }
    path = _document(tmp_path, document)
    with pytest.raises(ValidationError):
        ModelHostRoleBaselines.model_validate(yaml.safe_load(path.read_text("utf-8")))


def test_host_role_baseline_shipped_overlay_example_validates() -> None:
    document = yaml.safe_load(
        (_HOST_ROLE_BASELINE_DIR / "overlay.example.yaml").read_text("utf-8")
    )
    model = ModelHostRoleBaselines.model_validate(document)
    assert model.event_bus == ModelNodeEventBus(type="kafka", lane="example-lane")


def test_model_accepts_event_bus_key_failing_control(tmp_path: Path) -> None:
    """Failing control: at the base commit extra=forbid refused the key."""
    document = {**_required_document(), "event_bus": {"type": "kafka", "lane": "dev"}}
    path = _document(tmp_path, document)
    model = ModelHostRoleBaselines.model_validate(
        yaml.safe_load(path.read_text("utf-8"))
    )
    assert model.event_bus == ModelNodeEventBus(type="kafka", lane="dev")
