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
            loaded = yaml.load(
                path.read_text(encoding="utf-8"),
                Loader=getattr(yaml, "CSafeLoader", yaml.SafeLoader),
            )
            if not isinstance(loaded, dict):
                raise ValueError("contract must be a mapping")
            name = loaded.get("name")
            bus = loaded.get("event_bus")
            if not isinstance(name, str) or not name or not isinstance(bus, dict):
                continue
            for key, direction in _KEYS:
                for topic in bus.get(key) or []:
                    if isinstance(topic, str) and topic:
                        uses.add(
                            ModelContractTopicUse(
                                node=name, topic=topic, direction=direction
                            )
                        )
        except (ValueError, OSError, yaml.YAMLError) as error:
            raise ValueError(f"{path}: {error}") from error
    return tuple(sorted(uses, key=lambda u: (u.node, u.direction.value, u.topic)))
