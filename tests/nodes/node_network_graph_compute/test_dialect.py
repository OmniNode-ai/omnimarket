# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""N1.1: the reader reads every topic dialect; a reader that skips one fails its fixture."""

from __future__ import annotations

import pytest
import yaml

from omnimarket.models.network_graph import EnumTopicDirection, ModelDialect
from omnimarket.nodes.node_network_graph_compute.handlers.contract_reader import (
    read_declared_topics,
)
from omnimarket.nodes.node_network_graph_compute.handlers.dialect_table import (
    DIALECT_TABLE,
)

pytestmark = pytest.mark.unit

TOPIC = "onex.evt.fixture.dialect.v1"

# One contract body per dialect of the table, keyed by the dialect's dotted path.
FIXTURES: dict[str, str] = {
    "event_bus.publish_topics": f"event_bus:\n  publish_topics:\n    - {TOPIC}\n",
    "event_bus.subscribe_topics": f"event_bus:\n  subscribe_topics:\n    - {TOPIC}\n",
    "publish_topics": f"publish_topics:\n  - topic: {TOPIC}\n    description: d\n",
    "subscribe_topics": f"subscribe_topics:\n  - topic: {TOPIC}\n    description: d\n",
    "published_events": (
        f"published_events:\n  - event_type: ModelX\n    topic: {TOPIC}\n"
    ),
    "consumed_events": (
        f"consumed_events:\n  - event_type: ModelX\n    topic: {TOPIC}\n"
    ),
    "input_subscriptions": (
        f"input_subscriptions:\n  - topic: {TOPIC}\n    operation: run\n"
    ),
    "externally_consumed_topics": f"externally_consumed_topics:\n  - {TOPIC}\n",
    "externally_produced_topics": (
        f"externally_produced_topics:\n  - topic: {TOPIC}\n    producer: a script\n"
    ),
    "event_bus.publish": f"event_bus:\n  publish:\n    success_topic: {TOPIC}\n",
    "event_bus.subscribe": f"event_bus:\n  subscribe:\n    topic: {TOPIC}\n",
    "event_bus.publish_topic_metadata": (
        f"event_bus:\n  publish_topic_metadata:\n    {TOPIC}:\n      kind: event\n"
    ),
    "event_bus.subscribe_topic_metadata": (
        f"event_bus:\n  subscribe_topic_metadata:\n    {TOPIC}:\n      kind: cmd\n"
    ),
    "published_events.event_pattern": (
        f"published_events:\n  - event_pattern: {TOPIC}\n"
    ),
    "consumed_events.event_pattern": (
        f"consumed_events:\n  - event_pattern: {TOPIC}\n    handler_function: h\n"
    ),
}


def _dotted(dialect: ModelDialect) -> str:
    return ".".join(dialect.path)


def _fixture_for(dialect: ModelDialect) -> str:
    return FIXTURES[_dotted(dialect)]


def _read(body: str, table: tuple[ModelDialect, ...]) -> dict[str, tuple[str, ...]]:
    topics = read_declared_topics(yaml.safe_load(body), table)
    return {
        "publishes": tuple(sorted(topics.publishes)),
        "subscribes": tuple(sorted(topics.subscribes)),
    }


def test_dialect_every_table_entry_has_a_fixture() -> None:
    assert {_dotted(d) for d in DIALECT_TABLE} <= set(FIXTURES)


def test_dialect_table_covers_the_ticket_dialects() -> None:
    required = {
        "published_events",
        "consumed_events",
        "input_subscriptions",
        "event_bus.publish",
        "event_bus.subscribe",
        "event_bus.publish_topic_metadata",
        "event_bus.subscribe_topic_metadata",
        "externally_consumed_topics",
        "externally_produced_topics",
    }
    assert required <= {_dotted(d) for d in DIALECT_TABLE}


@pytest.mark.parametrize("dialect", DIALECT_TABLE, ids=_dotted)
def test_dialect_fixture_yields_its_topic_in_its_direction(
    dialect: ModelDialect,
) -> None:
    got = _read(_fixture_for(dialect), DIALECT_TABLE)
    if dialect.direction is EnumTopicDirection.PUBLISH:
        assert got == {"publishes": (TOPIC,), "subscribes": ()}
    else:
        assert got == {"publishes": (), "subscribes": (TOPIC,)}


@pytest.mark.parametrize("dialect", DIALECT_TABLE, ids=_dotted)
def test_dialect_planted_reader_skipping_this_dialect_fails_its_fixture(
    dialect: ModelDialect,
) -> None:
    planted = tuple(d for d in DIALECT_TABLE if d is not dialect)
    got = _read(_fixture_for(dialect), planted)
    assert got == {"publishes": (), "subscribes": ()}


def test_dialect_a_malformed_dialect_value_yields_nothing_and_does_not_raise() -> None:
    body = "event_bus:\n  publish_topics: 7\npublished_events: {a: 1}\n"
    assert _read(body, DIALECT_TABLE) == {"publishes": (), "subscribes": ()}


def test_dialect_all_dialects_in_one_contract_union() -> None:
    merged = {
        "event_bus": {
            "publish_topics": ["t.a"],
            "subscribe_topics": ["t.b"],
            "publish": {"success_topic": "t.c"},
        },
        "externally_consumed_topics": ["t.d"],
        "externally_produced_topics": [{"topic": "t.e", "producer": "p"}],
    }
    topics = read_declared_topics(merged, DIALECT_TABLE)
    assert topics.publishes == frozenset({"t.a", "t.c", "t.e"})
    assert topics.subscribes == frozenset({"t.b", "t.d"})
