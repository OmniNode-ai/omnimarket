# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Config model for the user-correction observer EFFECT node (OMN-12846).

Topic defaults mirror the ``event_bus`` declarations in this node's
``contract.yaml`` — the contract is the source of truth. The handler resolves
the publish topic from this config, never hardcoding a topic literal.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

# Topic suffixes mirroring contract.yaml event_bus (source of truth).
TOPIC_USER_CORRECTION_OBSERVED = "onex.cmd.omnimarket.user-correction-observed.v1"  # onex-topic-allow: contract-mirrored default
TOPIC_USER_CORRECTION = "onex.evt.omnimarket.user-correction.v1"  # onex-topic-allow: contract-mirrored default


class ModelUserCorrectionObserverConfig(BaseModel):
    """Configuration for the user-correction observer.

    Topic suffix defaults MUST match the ``event_bus.subscribe_topics`` and
    ``event_bus.publish_topics`` declared in this node's ``contract.yaml``. The
    contract is the source of truth for topic declarations.
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    subscribe_topics: list[str] = Field(
        default_factory=lambda: [TOPIC_USER_CORRECTION_OBSERVED],
        description="Topic suffixes to subscribe to (env prefix added at runtime)",
    )
    publish_topics: list[str] = Field(
        default_factory=lambda: [TOPIC_USER_CORRECTION],
        description="Topic suffixes to publish to (env prefix added at runtime)",
    )


__all__ = [
    "ModelUserCorrectionObserverConfig",
]
