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


def test_contract_routes_census_operation_and_declares_no_unpublished_topic() -> None:
    contract = yaml.safe_load(
        Path(
            "src/omnimarket/nodes/node_consumer_group_guard_effect/contract.yaml"
        ).read_text()
    )
    # No code publishes the census yet, so the contract must not declare it (publish-parity gate).
    publish_topics = contract["event_bus"]["publish_topics"]
    assert "onex.evt.omnimarket.consumer-group-census.v1" not in publish_topics
    externally_consumed = contract["externally_consumed_topics"]
    assert "onex.evt.omnimarket.consumer-group-census.v1" not in externally_consumed
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


def _census(
    states: dict[str, str], reader: Any, patterns: tuple[str, ...] = ("projection",)
) -> Any:
    handler = HandlerConsumerGroupCensusEvent(
        group_lister=lambda _config: states,
        broker_resolver=lambda _lane: ("broker", "env", {}),
        lag_reader=reader,
        lag_patterns=patterns,
    )
    return asyncio.run(
        handler.handle(ModelConsumerGroupGuardRequest(bus_lane="lane-a"))
    )


def test_census_carries_the_lag_of_watched_groups_that_are_not_empty() -> None:
    asked: list[list[str]] = []

    def reader(_config: Any, names: list[str]) -> dict[str, int]:
        asked.append(names)
        return dict.fromkeys(names, 42)

    event = _census(
        {
            "p.projection.a": "STABLE",
            "p.projection.empty": "EMPTY",
            "other": "STABLE",
        },
        reader,
    )
    assert asked == [["p.projection.a"]]
    assert event.lag == {"p.projection.a": 42}


def test_a_failed_lag_read_still_publishes_the_census_with_no_lag() -> None:
    def reader(_config: Any, _names: list[str]) -> dict[str, int]:
        raise RuntimeError("secret-token")

    event = _census({"p.projection.a": "STABLE"}, reader)
    assert event.lag == {}
    assert event.groups == {"p.projection.a": "STABLE"}


def test_no_watched_group_means_the_lag_reader_is_not_called() -> None:
    def reader(_config: Any, _names: list[str]) -> dict[str, int]:
        raise AssertionError("must not be called")

    assert _census({"other": "STABLE"}, reader).lag == {}


def test_lag_flows_from_the_census_through_the_reducer(
    overlay: ModelGroupStateOverlay,
) -> None:
    bar = overlay.lag_bar_messages
    event = _census(
        {"p.projection.a": "STABLE", "p.projection.b": "STABLE"},
        lambda _c, _names: {"p.projection.a": bar + 1, "p.projection.b": 0},
    )
    record = fold(
        None, ModelGroupCensusEvent.model_validate(event.model_dump()), overlay
    )
    assert record.lagging == ("p.projection.a",)


def test_the_contract_declares_the_lag_group_patterns() -> None:
    from omnimarket.nodes.node_consumer_group_guard_effect.handlers.handler_consumer_group_guard import (
        lag_group_patterns,
    )

    patterns = lag_group_patterns()
    assert patterns
    assert any(
        __import__("re").search(p, "node_projection_delegation") for p in patterns
    )
