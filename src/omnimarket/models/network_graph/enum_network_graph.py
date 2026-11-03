# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Enums of the node network graph document."""

from __future__ import annotations

from enum import StrEnum


class EnumTopicDirection(StrEnum):
    """Whether a contract declaration makes its node a publisher or a subscriber."""

    PUBLISH = "publish"
    SUBSCRIBE = "subscribe"


class EnumDialectShape(StrEnum):
    """How a dialect lays its topics out under its key path."""

    #: A list whose items are topic strings or mappings carrying a topic.
    LIST = "list"
    #: A mapping whose values are the topics (a publish or subscribe mapping).
    MAPPING_VALUES = "mapping_values"
    #: A mapping keyed by topic (a mapping-shaped topic list).
    MAPPING_KEYS = "mapping_keys"


class EnumVertexState(StrEnum):
    """The state of a vertex. This half of the graph only ever sets UNKNOWN."""

    UNKNOWN = "unknown"


class EnumVertexCause(StrEnum):
    """The cause carried with a state."""

    CONTRACT_UNREADABLE = "contract-unreadable"
