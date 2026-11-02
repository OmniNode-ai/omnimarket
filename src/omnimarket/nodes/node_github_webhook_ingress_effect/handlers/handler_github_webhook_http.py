# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed webhook HTTP boundary: verify raw bytes, then publish to the existing node."""

from __future__ import annotations

import asyncio
import base64
import json
from datetime import UTC, datetime
from uuid import UUID, uuid4

from omnimarket.nodes.node_github_webhook_ingress_effect.models.model_github_webhook_http import (
    ModelWebhookHttpConfig,
    ModelWebhookHttpRequest,
    ModelWebhookHttpResponse,
)
from omnimarket.nodes.node_github_webhook_ingress_effect.protocols.protocol_webhook_http import (
    ProtocolWebhookConfiguration,
    ProtocolWebhookPublisher,
)
from omnimarket.nodes.node_github_webhook_ingress_effect.webhook_fold import (
    verify_signature,
)


class HandlerGitHubWebhookHttp:
    """Preserve the HTTP refusal order and acknowledge only a completed publish.

    Delivery IDs remain the key and correlation ID on redelivery. The existing
    consumer verifies again and provides its deterministic observation IDs.
    """

    def __init__(
        self,
        *,
        configuration: ProtocolWebhookConfiguration,
        publisher: ProtocolWebhookPublisher,
        config: ModelWebhookHttpConfig,
    ) -> None:
        self._configuration = configuration
        self._publisher = publisher
        self._config = config

    async def handle(
        self, request: ModelWebhookHttpRequest
    ) -> ModelWebhookHttpResponse:
        secret, target = await self._configuration.resolve()
        if not secret or target is None:
            return self._refuse(503, "github webhook ingress is not configured")
        if not request.event or not request.delivery_id or not request.signature:
            return self._refuse(400, "missing GitHub delivery headers")
        try:
            delivery_id = UUID(request.delivery_id)
        except ValueError:
            return self._refuse(400, "delivery id is not a GUID")
        if len(request.body) > self._config.max_body_bytes:
            return self._refuse(413, "delivery body too large")
        if not verify_signature(secret, request.body, request.signature):
            return self._refuse(401, "signature does not verify")
        if request.event == "ping":
            return ModelWebhookHttpResponse(
                status_code=200, body={"ok": True, "event": "ping"}
            )

        stamp = datetime.now(UTC).isoformat()
        # This is the existing delivery envelope after the forwarder's tenant
        # stamp. On the runtime bus the HTTP mount needs no tenant-wire detour.
        envelope = {
            "envelope_id": str(uuid4()),
            "envelope_timestamp": stamp,
            "correlation_id": str(delivery_id),
            "tenant_id": target.tenant_slug,
            "source_tool": self._config.source_tool,
            "event_type": self._config.event_type,
            "priority": 5,
            "retry_count": 0,
            "onex_version": {"major": 1, "minor": 0, "patch": 0},
            "envelope_version": {"major": 2, "minor": 1, "patch": 0},
            "metadata": {
                "tags": {
                    "source_tenant_id": str(target.tenant_id),
                    "source_tenant_principal_id": target.principal_id,
                    "github_event": request.event,
                    "github_delivery_id": str(delivery_id),
                }
            },
            "payload": {
                "event": request.event,
                "delivery_id": str(delivery_id),
                "signature_256": request.signature,
                "body_b64": base64.b64encode(request.body).decode("ascii"),
                "received_at": stamp,
                "tenant_id": target.tenant_slug,
            },
        }
        try:
            async with asyncio.timeout(self._config.publish_timeout_seconds):
                await self._publisher.publish(
                    topic=self._config.delivery_topic,
                    key=str(delivery_id).encode(),
                    value=json.dumps(envelope).encode(),
                )
        except Exception:
            # The boundary never puts a transport exception, raw body, signature
            # or signing material into a log or an HTTP response.
            return self._refuse(503, "delivery not published")
        return ModelWebhookHttpResponse(
            status_code=202, body={"ok": True, "delivery_id": str(delivery_id)}
        )

    @staticmethod
    def _refuse(status: int, error: str) -> ModelWebhookHttpResponse:
        return ModelWebhookHttpResponse(
            status_code=status, body={"ok": False, "error": error}
        )
