# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The places a node contract may declare topics, one row per dialect.

The reader walks this table and nothing else, so a dialect is added by adding a row.
A row names a key path, the layout under it and the direction its topics have.
An `externally_*` declaration counts as a declaration of that direction.
"""

from __future__ import annotations

from omnimarket.models.network_graph import (
    EnumDialectShape,
    EnumTopicDirection,
    ModelDialect,
)

_PUBLISH = EnumTopicDirection.PUBLISH
_SUBSCRIBE = EnumTopicDirection.SUBSCRIBE
_LIST = EnumDialectShape.LIST
_VALUES = EnumDialectShape.MAPPING_VALUES
_KEYS = EnumDialectShape.MAPPING_KEYS

#: The keys of a mapping item that carry its topic, in the order they are tried.
ITEM_TOPIC_KEYS: tuple[str, ...] = ("topic", "event_pattern", "full_topic_pattern")

DIALECT_TABLE: tuple[ModelDialect, ...] = (
    ModelDialect(direction=_PUBLISH, path=("event_bus", "publish_topics"), shape=_LIST),
    ModelDialect(direction=_PUBLISH, path=("publish_topics",), shape=_LIST),
    ModelDialect(direction=_PUBLISH, path=("published_events",), shape=_LIST),
    ModelDialect(direction=_PUBLISH, path=("externally_produced_topics",), shape=_LIST),
    ModelDialect(direction=_PUBLISH, path=("event_bus", "publish"), shape=_VALUES),
    ModelDialect(
        direction=_PUBLISH, path=("event_bus", "publish_topic_metadata"), shape=_KEYS
    ),
    ModelDialect(
        direction=_SUBSCRIBE, path=("event_bus", "subscribe_topics"), shape=_LIST
    ),
    ModelDialect(direction=_SUBSCRIBE, path=("subscribe_topics",), shape=_LIST),
    ModelDialect(direction=_SUBSCRIBE, path=("consumed_events",), shape=_LIST),
    ModelDialect(direction=_SUBSCRIBE, path=("subscribed_events",), shape=_LIST),
    ModelDialect(direction=_SUBSCRIBE, path=("subscriptions", "topics"), shape=_LIST),
    ModelDialect(direction=_SUBSCRIBE, path=("input_subscriptions",), shape=_LIST),
    ModelDialect(
        direction=_SUBSCRIBE, path=("externally_consumed_topics",), shape=_LIST
    ),
    ModelDialect(direction=_SUBSCRIBE, path=("event_bus", "subscribe"), shape=_VALUES),
    ModelDialect(
        direction=_SUBSCRIBE,
        path=("event_bus", "subscribe_topic_metadata"),
        shape=_KEYS,
    ),
)
