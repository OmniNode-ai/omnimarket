# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The declared lane dispatch node through the runtime bus and command adapters."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from omnimarket.models.lane_dispatch.model_lane_dispatch import ModelLaneDispatchRecord
from omnimarket.nodes.node_lane_dispatch_effect.runtime_lane_dispatch_cli import (
    dispatch,
)
from tests.nodes.test_lane_dispatch_contract import configure_runtime


def test_declared_node_over_inmemory_overlay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = configure_runtime(tmp_path, monkeypatch)
    result = dispatch(request)
    assert result.status == "submitted", result
    observed = json.loads((tmp_path / "runner-observed.json").read_text())
    assert observed[observed.index("--host") + 1] == "h053"
    assert (
        result.content_hash
        == ModelLaneDispatchRecord.model_validate_json(request.record_json).content_hash
    )


def test_declared_completed_topic_is_terminal_and_published() -> None:
    contract = yaml.safe_load(
        (
            Path(__file__).parents[2]
            / "src/omnimarket/nodes/node_lane_dispatch_effect/contract.yaml"
        ).read_text()
    )
    assert contract["event_bus"]["publish_topics"] == [
        "onex.evt.omnimarket.lane-dispatch-completed.v1"
    ]
    assert (
        contract["terminal_event"] == "onex.evt.omnimarket.lane-dispatch-completed.v1"
    )


@pytest.mark.kafka
@pytest.mark.integration
def test_same_declared_node_over_redpanda_overlay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.bus_lane_guard import (
        declared_test_lane,
        resolve_test_bus_broker,
    )

    broker = resolve_test_bus_broker()
    request = configure_runtime(
        tmp_path, monkeypatch, bus="kafka", broker=broker, bus_lane=declared_test_lane()
    )
    result = dispatch(request)
    assert result.status == "submitted", result
    observed = json.loads((tmp_path / "runner-observed.json").read_text())
    assert observed[observed.index("--host") + 1] == "h053"
    assert (
        result.content_hash
        == ModelLaneDispatchRecord.model_validate_json(request.record_json).content_hash
    )
