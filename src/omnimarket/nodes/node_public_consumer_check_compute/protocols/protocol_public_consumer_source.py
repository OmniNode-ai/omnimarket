# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The injected search dependency of the public-consumer check (OMN-23145)."""

from __future__ import annotations

from typing import Protocol

from omnimarket.nodes.node_public_consumer_check_compute.models.model_public_consumer_check import (
    ModelSearchScope,
    ModelSourceHit,
)


class PublicConsumerSourceError(Exception):
    """A scope could not be read; the check keeps the entity rather than read it as clear."""


class ProtocolPublicConsumerSource(Protocol):
    def search(
        self, scope: ModelSearchScope, reference: str
    ) -> tuple[ModelSourceHit, ...]:
        """Return every place ``reference`` appears in ``scope``; raise on an unreadable scope."""
        ...
