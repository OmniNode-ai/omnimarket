# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for the census-event producer of node_consumer_group_guard_effect (OMN-23992)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import yaml

from omnimarket.models.consumer_group_guard import ModelConsumerGroupGuardRequest
from omnimarket.nodes.node_consumer_group_guard_effect.handlers.handler_consumer_group_census_event import (
    ConsumerGroupCensusUnreadableError,
    HandlerConsumerGroupCensusEvent,
)
from omnimarket.nodes.node_consumer_group_state_reducer.handlers.handler_group_state import (
    fold,
)
from omnimarket.nodes.node_consumer_group_state_reducer.models.model_group_state import (
    ModelGroupCensusEvent,
)
from omnimarket.nodes.node_consumer_group_state_reducer.overlay import (
    ModelGroupStateOverlay,
)


@pytest.fixture
def overlay() -> ModelGroupStateOverlay:
    path = Path(
        "src/omnimarket/nodes/node_consumer_group_state_reducer/overlay.example.yaml"
    )
    raw = yaml.safe_load(path.read_text())
    return ModelGroupStateOverlay.model_validate(raw)


def test_golden_census_event() -> None:
    groups = {"a.0123456789ab": "EMPTY", "b": "STABLE"}
    fixed_time = datetime(2026, 1, 1, tzinfo=UTC)
    handler = HandlerConsumerGroupCensusEvent(
        group_lister=lambda _config: groups,
        broker_resolver=lambda _lane: ("broker", "env", {}),
        clock=lambda: fixed_time,
    )
    request = ModelConsumerGroupGuardRequest(bus_lane="lane-a")
    event = asyncio.run(handler.handle(request))
    assert event.groups == groups
    assert event.bus_lane == "lane-a"
    assert event.observed_at == fixed_time


def test_error_listing_does_not_leak_secret() -> None:
    def _raise(_config: Any) -> dict[str, str]:
        raise RuntimeError("secret-token")

    handler = HandlerConsumerGroupCensusEvent(
        group_lister=_raise,
        broker_resolver=lambda _lane: ("broker", "env", {}),
        clock=lambda: datetime.now(UTC),
    )
    request = ModelConsumerGroupGuardRequest(bus_lane="lane-a")
    with pytest.raises(ConsumerGroupCensusUnreadableError) as exc_info:
        asyncio.run(handler.handle(request))
    assert "secret-token" not in str(exc_info.value)


def test_empty_listing_is_refused() -> None:
    handler = HandlerConsumerGroupCensusEvent(
        group_lister=lambda _config: {},
        broker_resolver=lambda _lane: ("broker", "env", {}),
        clock=lambda: datetime.now(UTC),
    )
    request = ModelConsumerGroupGuardRequest(bus_lane="lane-a")
    with pytest.raises(ConsumerGroupCensusUnreadableError) as exc_info:
        asyncio.run(handler.handle(request))
    assert "empty" in str(exc_info.value)


def test_json_round_trip() -> None:
    event = ModelGroupCensusEvent(
        correlation_id=uuid4(),
        bus_lane="lane-a",
        observed_at=datetime(2026, 1, 1, tzinfo=UTC),
        groups={"a": "EMPTY"},
    )
    restored = ModelGroupCensusEvent.model_validate_json(event.model_dump_json())
    assert restored == event


def test_seam_folds_through_reducer() -> None:
    groups = {"a.0123456789ab": "EMPTY", "b": "STABLE"}
    fixed_time = datetime(2026, 1, 1, tzinfo=UTC)
    handler = HandlerConsumerGroupCensusEvent(
        group_lister=lambda _config: groups,
        broker_resolver=lambda _lane: ("broker", "env", {}),
        clock=lambda: fixed_time,
    )
    request = ModelConsumerGroupGuardRequest(bus_lane="lane-a")
    event = asyncio.run(handler.handle(request))
    overlay = ModelGroupStateOverlay.model_validate(
        yaml.safe_load(
            Path(
                "src/omnimarket/nodes/node_consumer_group_state_reducer/overlay.example.yaml"
            ).read_text()
        )
    )
    record = fold(None, event, overlay)
    assert {g.name: g.state for g in record.groups} == groups


def test_renamed_field_is_refused() -> None:
    with pytest.raises(ValueError, match="groups"):
        ModelGroupCensusEvent.model_validate(
            {
                "correlation_id": uuid4(),
                "bus_lane": "lane-a",
                "observed_at": datetime(2026, 1, 1, tzinfo=UTC),
                "group_states": {"a": "EMPTY"},
            }
        )


def test_contract_lists_census_topic_and_routes_new_operation() -> None:
    contract = yaml.safe_load(
        Path(
            "src/omnimarket/nodes/node_consumer_group_guard_effect/contract.yaml"
        ).read_text()
    )
    publish_topics = contract["event_bus"]["publish_topics"]
    assert "onex.evt.omnimarket.consumer-group-census.v1" in publish_topics
    externally_consumed = contract["externally_consumed_topics"]
    assert "onex.evt.omnimarket.consumer-group-census.v1" in externally_consumed
    handlers = contract["handler_routing"]["handlers"]
    census = next(
        h for h in handlers if h["operation"] == "consumer_group_census_event_effect"
    )
    assert census["handler"]["name"] == "HandlerConsumerGroupCensusEvent"
    assert (
        census["handler"]["module"]
        == "omnimarket.nodes.node_consumer_group_guard_effect.handlers.handler_consumer_group_census_event"
    )
    import importlib

    module = importlib.import_module(census["handler"]["module"])
    assert hasattr(module, census["handler"]["name"])
