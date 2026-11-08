# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Port of node_secret_rotation_readback_effect: one consumer completes one call."""

from __future__ import annotations

from typing import Protocol

from omnimarket.models.model_secret_rotation import EnumConsumerCallOutcome
from omnimarket.nodes.node_secret_rotation_readback_effect.overlay import (
    ModelRotationConsumer,
)


class ProtocolConsumerCalls(Protocol):
    """Make the consumer complete one call with the key it holds now.

    The consumer holds its own key; the readback never hands it one. An implementation never
    raises: a consumer it cannot reach is ``unreachable``.
    """

    def complete_call(
        self, consumer: ModelRotationConsumer
    ) -> EnumConsumerCallOutcome: ...


__all__ = ["ProtocolConsumerCalls"]
