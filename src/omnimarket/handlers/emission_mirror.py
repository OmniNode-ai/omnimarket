# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The journal sink's mirror onto the bus (OMN-23877): each chain event the journal holds is also published on its phase's topic.

The floor emissions projection folds those topics, so the board reads real work from the bus and the file on one machine is health
only. The journal write comes first and never depends on the mirror: a mirror that fails is counted by the sink and the chain goes on.
A host whose overlay (``ONEX_FLOOR_EMISSION_MIRROR_OVERLAY`` or ``floor_emission_mirror`` under the overlay roots) is absent mirrors
nothing, so this code and the overlay merge in either order.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractAsyncContextManager
from typing import Any, Literal, Protocol

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    NodeOverlayNotFoundError,
    load_structured_overlay,
)
from omnimarket.handlers.overlay_store.bus_opener import open_lane_bus, workspace_root
from omnimarket.models.floor_17a import ModelNodeEmission
from omnimarket.models.floor_projection import ModelEmissionMirrorOverlay

OVERLAY_ID = "floor_emission_mirror"
OVERLAY_FILE_ENV = "ONEX_FLOOR_EMISSION_MIRROR_OVERLAY"

MirrorState = Literal["mirroring", "not_configured", "unreadable"]


class EmissionMirrorRefusedError(Exception):
    """The bus refused one mirrored chain event: the topic it was refused on and what the bus said, so the sink can write one line."""

    def __init__(self, topic: str, cause: BaseException) -> None:
        self.topic = topic
        self.error_type = type(cause).__name__
        self.detail = str(cause)[:200]
        super().__init__(f"topic={topic} error={self.error_type} detail={self.detail}")


class ProtocolEmissionMirror(Protocol):
    def publish(self, event: ModelNodeEmission) -> None: ...


class InMemoryEmissionMirror:
    """Holds each mirrored event with the topic it would go to; the default when the overlay's bus is in memory."""

    def __init__(self, overlay: ModelEmissionMirrorOverlay) -> None:
        self._overlay = overlay
        self.published: list[tuple[str, ModelNodeEmission]] = []

    def publish(self, event: ModelNodeEmission) -> None:
        self.published.append((self._overlay.topic_for(event.phase), event))


class BusEmissionMirror:
    """The real backend: one publication of the event's JSON on its phase's topic, keyed by the chain's correlation id.

    The publication runs in a worker thread under its own loop, so it is safe to call from a running event loop and from none.
    """

    def __init__(
        self,
        overlay: ModelEmissionMirrorOverlay,
        open_bus: Callable[[], AbstractAsyncContextManager[Any]],
    ) -> None:
        self._overlay = overlay
        self._open_bus = open_bus

    async def _publish(self, event: ModelNodeEmission) -> None:
        async with self._open_bus() as bus:
            await bus.publish(
                self._overlay.topic_for(event.phase),
                event.correlation_id.encode("utf-8"),
                event.model_dump_json().encode("utf-8"),
            )

    def publish(self, event: ModelNodeEmission) -> None:
        with ThreadPoolExecutor(max_workers=1) as pool:
            try:
                pool.submit(
                    asyncio.run,
                    asyncio.wait_for(
                        self._publish(event), timeout=self._overlay.publish_timeout_s
                    ),
                ).result()
            except Exception as exc:
                raise EmissionMirrorRefusedError(
                    self._overlay.topic_for(event.phase), exc
                ) from exc


def build_mirror(
    overlay: ModelEmissionMirrorOverlay, env: Mapping[str, str]
) -> ProtocolEmissionMirror:
    """The mirror the overlay selects: held in memory, or the broker."""
    if overlay.bus == "inmemory":
        return InMemoryEmissionMirror(overlay)
    bootstrap = overlay.kafka_bootstrap
    return BusEmissionMirror(
        overlay,
        lambda: open_lane_bus(
            bus="kafka",
            lane=None if bootstrap else overlay.bus_lane,
            kafka_bootstrap=bootstrap,
            root=workspace_root(env),
        ),
    )


def mirror_from_environment(
    env: Mapping[str, str],
) -> tuple[ProtocolEmissionMirror | None, MirrorState]:
    """The mirror this host's overlay names, and how it stands: no overlay is ``not_configured`` and an invalid one ``unreadable``."""
    try:
        overlay = load_structured_overlay(
            OVERLAY_ID, ModelEmissionMirrorOverlay, env, file_env=OVERLAY_FILE_ENV
        )
    except NodeOverlayNotFoundError:
        return None, "not_configured"
    except NodeOverlayError:
        return None, "unreadable"
    return build_mirror(overlay, env), "mirroring"


__all__ = [
    "BusEmissionMirror",
    "EmissionMirrorRefusedError",
    "InMemoryEmissionMirror",
    "MirrorState",
    "ProtocolEmissionMirror",
    "build_mirror",
    "mirror_from_environment",
]
