# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The topics node_floor_emission_compute declares: it subscribes to nothing and runs in process (OMN-24298)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.models.floor_17a.model_floor_17a import (
    ModelFloorReceiptInput,
    ModelNodeEmission,
)

pytestmark = pytest.mark.unit

EMISSION_TOPICS = (
    "onex.evt.omnimarket.floor-node-started.v1",
    "onex.evt.omnimarket.floor-node-succeeded.v1",
    "onex.evt.omnimarket.floor-node-failed.v1",
)

CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_floor_emission_compute/contract.yaml"
)


def _bus() -> dict[str, list[str]]:
    loaded = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    bus: dict[str, list[str]] = loaded["event_bus"]
    return bus


def _node_contract(node: str) -> dict[str, Any]:
    loaded = yaml.safe_load(
        (CONTRACT.parents[1] / node / "contract.yaml").read_text(encoding="utf-8")
    )
    contract: dict[str, Any] = loaded
    return contract


def test_emission_subscribes_to_nothing_and_publishes_nothing() -> None:
    bus = _bus()
    assert bus["subscribe_topics"] == []
    assert bus["publish_topics"] == []


def test_emission_runs_in_process_for_its_caller() -> None:
    loaded = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    assert loaded["metadata"]["transport_type"] == "local"
    assert loaded["descriptor"]["runtime_profiles"] == []
    assert "runtime_profiles" not in loaded
    assert "terminal_event" not in loaded
    assert "node_floor_simple_effect" in loaded["runtime_dispatch"]["external_trigger"]


def test_single_event_is_refused_by_the_input_model() -> None:
    emission = ModelNodeEmission(
        node="node_lab_state_x",
        piece=1,
        correlation_id="c1",
        phase="start",
        emitted_at=datetime(2026, 11, 13, tzinfo=UTC),
    )
    with pytest.raises(ValidationError) as exc_info:
        ModelFloorReceiptInput.model_validate(emission.model_dump(mode="json"))
    locations = {
        ".".join(str(part) for part in error["loc"])
        for error in exc_info.value.errors()
    }
    assert {"now", "emissions", "pieces"} <= locations


def test_seam_emission_topics_stay_published_and_read_by_the_publisher() -> None:
    simple = _node_contract("node_floor_simple_effect")
    publisher = _node_contract("node_floor_emissions_publish_effect")
    compute = _node_contract("node_floor_emission_compute")
    for topic in EMISSION_TOPICS:
        assert topic in simple["event_bus"]["publish_topics"]
        assert topic in simple["externally_consumed_topics"]
        assert topic in publisher["snapshot_reads"]
    assert compute["event_bus"]["subscribe_topics"] == []
    assert not set(EMISSION_TOPICS) & set(compute["event_bus"]["publish_topics"])
