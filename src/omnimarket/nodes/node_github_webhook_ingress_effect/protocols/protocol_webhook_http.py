# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Configuration and acknowledged publication are injected at the HTTP boundary."""

from __future__ import annotations

from typing import Protocol

from omnimarket.nodes.node_github_webhook_ingress_effect.models.model_github_webhook_http import (
    ModelWebhookTarget,
)


class ProtocolWebhookConfiguration(Protocol):
    async def resolve(self) -> tuple[bytes, ModelWebhookTarget | None]:
        """Resolve the signing material and deployment routing identity."""
        ...


class ProtocolWebhookPublisher(Protocol):
    async def publish(self, *, topic: str, key: bytes, value: bytes) -> None:
        """Return only after the bus acknowledges the record, or raise."""
        ...
