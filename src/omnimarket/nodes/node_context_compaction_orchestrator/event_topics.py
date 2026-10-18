# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Class-name to topic routing of the compaction orchestrator's emissions (OMN-21113).

The runtime publishes each event the handler returns on the topic its
contract's ``published_events`` row names for the class (``event_type`` is the
class name minus its ``Model`` prefix). This reads that block from the contract,
so the topics live in one place, and :func:`publish_topic_for` is what a test
bus publishes through, so it carries each event on the topic the runtime would.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path

import yaml
from pydantic import BaseModel

_CONTRACT = Path(__file__).resolve().parent / "contract.yaml"


@cache
def published_event_topics() -> dict[str, str]:
    """``event_type`` to topic, from the contract's ``published_events``."""
    contract = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    return {row["event_type"]: row["topic"] for row in contract["published_events"]}


def publish_topic_for(event: BaseModel) -> str:
    """The contract topic the runtime publishes ``event`` on."""
    event_type = type(event).__name__.removeprefix("Model")
    try:
        return published_event_topics()[event_type]
    except KeyError as exc:
        msg = f"{event_type} is not an emission of node_context_compaction_orchestrator"
        raise ValueError(msg) from exc


__all__: list[str] = ["publish_topic_for", "published_event_topics"]
