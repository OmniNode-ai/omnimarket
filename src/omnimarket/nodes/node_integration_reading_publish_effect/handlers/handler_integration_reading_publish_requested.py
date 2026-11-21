# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The integration publication as a demand the runtime delivers (OMN-25036).

The hourly pass used to be the only caller, through the module that pipes one batch on stdin from a host. This handler
takes the same ``ModelIntegrationReadingBatch`` from the command topic the contract subscribes to, has
``node_integration_projection_compute`` build the projection, and publishes the floor projection envelope on the
snapshot topic over the runtime's own bus. The result is the terminal event of the demand. A projection id the overlay
cannot give, or a bus that refuses, is a typed result with ``published`` false and the reason, never a raise.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from importlib import resources
from typing import Any

import yaml

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    load_structured_overlay,
)
from omnimarket.models.floor_projection import (
    ModelFloorProjectionEnvelope,
    ModelFloorProjectionPublishOverlay,
    ModelFloorProjectionPublishResult,
)
from omnimarket.models.integration_reading import (
    ModelIntegrationPublishCommand,
    ModelIntegrationReadingBatch,
)
from omnimarket.nodes.node_integration_projection_compute.handlers import (
    HandlerIntegrationProjection,
)
from omnimarket.nodes.node_integration_reading_publish_effect.handlers.handler_integration_reading_publish import (
    HandlerIntegrationReadingPublish,
)

NODE = "node_integration_reading_publish_effect"


def _runtime_block() -> dict[str, Any]:
    text = (
        resources.files(f"omnimarket.nodes.{NODE}")
        .joinpath("contract.yaml")
        .read_text(encoding="utf-8")
    )
    return dict(yaml.safe_load(text)["runtime"])


class RuntimeBusEnvelopePublisher:
    """One publication of an envelope on the topic, over the bus the runtime hands the handler."""

    def __init__(self, bus: Any, topic: str) -> None:
        self._bus = bus
        self._topic = topic

    async def publish(self, envelope: ModelFloorProjectionEnvelope) -> None:
        """Keyed by the projection id, so a compacted topic keeps the newest publication of each projection."""
        await self._bus.publish(
            self._topic,
            key=envelope.projection_id.encode("utf-8"),
            value=envelope.model_dump_json().encode("utf-8"),
        )


class HandlerIntegrationReadingPublishRequested:
    """Compute and publish the integration projection of one demanded batch."""

    def __init__(
        self,
        event_bus: Any,
        overlay: ModelFloorProjectionPublishOverlay | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._bus: Any = event_bus
        self._overlay = overlay
        self._environ: Mapping[str, str] = (
            environ if environ is not None else os.environ
        )

    def _publish_overlay(self) -> ModelFloorProjectionPublishOverlay:
        if self._overlay is None:
            spec = _runtime_block()["overlay"]
            self._overlay = load_structured_overlay(
                spec["overlay_id"],
                ModelFloorProjectionPublishOverlay,
                self._environ,
                file_env=spec["file_env"],
            )
        return self._overlay

    async def handle(
        self, request: ModelIntegrationReadingBatch
    ) -> ModelFloorProjectionPublishResult:
        projection = HandlerIntegrationProjection().handle(request)
        try:
            overlay = self._publish_overlay()
        except NodeOverlayError as exc:
            return ModelFloorProjectionPublishResult(
                published=False,
                projection_id="integration",
                as_of=projection.as_of,
                entries=len(projection.rows),
                reason=f"integration_overlay_unreadable: {exc}"[:500],
            )
        publisher = RuntimeBusEnvelopePublisher(self._bus, _runtime_block()["topic"])
        handler = HandlerIntegrationReadingPublish(
            publisher=publisher,
            producer=f"{NODE}:{self._environ.get('HOSTNAME', '') or os.uname().nodename}",
        )
        try:
            return await handler.handle(
                ModelIntegrationPublishCommand(
                    projection_id=overlay.projection_id, projection=projection
                )
            )
        except Exception as exc:
            return ModelFloorProjectionPublishResult(
                published=False,
                projection_id=overlay.projection_id,
                as_of=projection.as_of,
                entries=len(projection.rows),
                reason=f"integration_publish_failed: {type(exc).__name__}",
            )


__all__ = ["HandlerIntegrationReadingPublishRequested", "RuntimeBusEnvelopePublisher"]
