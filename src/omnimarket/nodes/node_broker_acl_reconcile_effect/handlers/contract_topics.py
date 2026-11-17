# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read the topics every packaged node contract declares (OMN-19998, issue L1-04).

The reconcile effect owns the file read; the derive handler of the render node stays pure and takes the topics on its request.
"""

from importlib import resources
from pathlib import Path

import yaml

from omnimarket.models.broker_acl import (
    EnumTopicDirection,
    ModelContractTopicUse,
)

_KEYS = (
    ("subscribe_topics", EnumTopicDirection.SUBSCRIBE),
    ("publish_topics", EnumTopicDirection.PUBLISH),
)


def parse_topic_uses(text: str) -> tuple[ModelContractTopicUse, ...]:
    """The (node, topic, direction) uses one contract's event_bus declares; a contract with no name or event_bus declares none.

    The first subscribe topic is the node's primary command topic (the one an `onex run-node` request is sent to) and the
    contract's top-level `terminal_event`, when it is among the publish topics, the terminal topic; a terminal_event the
    publish topics do not carry is still a use of its own, so a caller's rows follow from the contract alone.
    """
    loaded = yaml.load(text, Loader=getattr(yaml, "CSafeLoader", yaml.SafeLoader))
    if not isinstance(loaded, dict):
        raise ValueError("contract must be a mapping")
    name = loaded.get("name")
    bus = loaded.get("event_bus")
    if not isinstance(name, str) or not name or not isinstance(bus, dict):
        return ()
    topics = {
        key: tuple(
            topic for topic in bus.get(key) or [] if isinstance(topic, str) and topic
        )
        for key, _ in _KEYS
    }
    terminal = loaded.get("terminal_event")
    if not isinstance(terminal, str) or not terminal:
        terminal = None
    uses = [
        ModelContractTopicUse(
            node=name,
            topic=topic,
            direction=EnumTopicDirection.SUBSCRIBE,
            command=index == 0,
        )
        for index, topic in enumerate(topics["subscribe_topics"])
    ]
    uses.extend(
        ModelContractTopicUse(
            node=name,
            topic=topic,
            direction=EnumTopicDirection.PUBLISH,
            terminal=topic == terminal,
        )
        for topic in topics["publish_topics"]
    )
    if terminal is not None and terminal not in topics["publish_topics"]:
        uses.append(
            ModelContractTopicUse(
                node=name,
                topic=terminal,
                direction=EnumTopicDirection.PUBLISH,
                terminal=True,
                declared=False,
            )
        )
    return tuple(uses)


def load_topic_uses(
    nodes_root: Path | None = None,
) -> tuple[ModelContractTopicUse, ...]:
    """Every (node, topic, direction) of every contract under the root, sorted; a contract that does not parse raises with its path."""
    root = (
        nodes_root
        if nodes_root is not None
        else Path(str(resources.files("omnimarket.nodes")))
    )
    uses: set[ModelContractTopicUse] = set()
    for path in sorted(root.rglob("contract.yaml")):
        try:
            uses.update(parse_topic_uses(path.read_text(encoding="utf-8")))
        except (ValueError, OSError, yaml.YAMLError) as error:
            raise ValueError(f"{path}: {error}") from error
    return tuple(sorted(uses, key=lambda u: (u.node, u.direction.value, u.topic)))
