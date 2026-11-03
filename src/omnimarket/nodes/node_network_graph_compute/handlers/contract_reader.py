# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure reading of one contract text: the plain text search and the dialect walk.

A vertex is found by text search, not by parsing, so a contract that carries a
`node_type` key and then fails to parse is still counted. Parsing only reads topics.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

import yaml

from omnimarket.models.network_graph import (
    EnumDialectShape,
    EnumTopicDirection,
    ModelDeclaredTopics,
    ModelDialect,
)
from omnimarket.nodes.node_network_graph_compute.handlers.dialect_table import (
    ITEM_TOPIC_KEYS,
)

_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)


def _top_level_line(key: str) -> re.Pattern[str]:
    return re.compile(rf"^{re.escape(key)}[ \t]*:(?P<value>[^\n]*)", re.MULTILINE)


def has_top_level_key(text: str, key: str) -> bool:
    """True when a line of the text opens with the key, at column 0, and a colon."""
    return _top_level_line(key).search(text) is not None


def find_top_level_scalar(text: str, key: str) -> str | None:
    """The scalar after a column-0 `key:`, less quotes and a trailing comment, or None."""
    found = _top_level_line(key).search(text)
    if found is None:
        return None
    value = re.sub(r"[ \t]+#.*$", "", found.group("value")).strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1].strip()
    return value or None


def parse_contract(text: str) -> Mapping[str, object] | None:
    """The contract as a mapping, or None when it does not parse as one."""
    try:
        loaded = yaml.load(text, Loader=_LOADER)
    except yaml.YAMLError:
        return None
    return loaded if isinstance(loaded, Mapping) else None


def _walk(data: object, path: Sequence[str]) -> object:
    for key in path:
        if not isinstance(data, Mapping):
            return None
        data = data.get(key)
    return data


def _item_topic(item: object) -> str | None:
    if isinstance(item, str):
        return item.strip() or None
    if isinstance(item, Mapping):
        for key in ITEM_TOPIC_KEYS:
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _topics_of(value: object, shape: EnumDialectShape) -> frozenset[str]:
    if shape is EnumDialectShape.LIST:
        items: Sequence[object] = value if isinstance(value, list) else []
        found = (_item_topic(item) for item in items)
    elif shape is EnumDialectShape.MAPPING_VALUES:
        values = value.values() if isinstance(value, Mapping) else ()
        found = (_item_topic(item) for item in values)
    else:
        keys = value.keys() if isinstance(value, Mapping) else ()
        found = (_item_topic(key) for key in keys)
    return frozenset(topic for topic in found if topic is not None)


def read_declared_topics(
    data: Mapping[str, object], table: Sequence[ModelDialect]
) -> ModelDeclaredTopics:
    """The topics a parsed contract declares in any dialect of the table, per direction."""
    publishes: set[str] = set()
    subscribes: set[str] = set()
    for dialect in table:
        topics = _topics_of(_walk(data, dialect.path), dialect.shape)
        if dialect.direction is EnumTopicDirection.PUBLISH:
            publishes |= topics
        else:
            subscribes |= topics
    return ModelDeclaredTopics(
        publishes=frozenset(publishes), subscribes=frozenset(subscribes)
    )
