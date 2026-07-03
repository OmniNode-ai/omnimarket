# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Config model for the user-correction observer EFFECT node (OMN-12846).

Topics are sourced from this node's ``contract.yaml`` (``event_bus`` block) —
the contract is the single source of truth. No topic literal is hardcoded in
source; the defaults are read from the contract file at construction time so the
imperative-contract guard sees no hardcoded topic string.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

_CONTRACT_PATH = Path(__file__).resolve().parent.parent / "contract.yaml"


def _contract_topics(field: str) -> list[str]:
    """Read ``event_bus.<field>`` topic list from the node contract.

    Fail-fast: a missing contract or missing topic list is a wiring error, not a
    silently-defaulted empty list.
    """
    contract = yaml.safe_load(_CONTRACT_PATH.read_text())
    event_bus = contract["event_bus"]
    topics = event_bus[field]
    if not isinstance(topics, list) or not topics:
        raise ValueError(
            f"contract.yaml event_bus.{field} must declare a non-empty topic list"
        )
    return [str(topic) for topic in topics]


class ModelUserCorrectionObserverConfig(BaseModel):
    """Configuration for the user-correction observer.

    Topic lists are sourced from this node's ``contract.yaml`` at construction
    time. The contract is the source of truth for topic declarations; there is
    no topic literal in this module.
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    subscribe_topics: list[str] = Field(
        default_factory=lambda: _contract_topics("subscribe_topics"),
        description="Topics to subscribe to, sourced from contract.yaml event_bus",
    )
    publish_topics: list[str] = Field(
        default_factory=lambda: _contract_topics("publish_topics"),
        description="Topics to publish to, sourced from contract.yaml event_bus",
    )


__all__ = [
    "ModelUserCorrectionObserverConfig",
]
