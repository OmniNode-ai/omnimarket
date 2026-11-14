# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HostAnnouncer: publish one host's announcements at start, on change, on a heartbeat and at stop (OMN-19998).

The reason of each announcement is the announcer's judgement: the first one that reaches the bus is ``start``, a later one
whose facts digest differs from the last published is ``change``, any other is ``heartbeat``, and the one published when the
process is told to stop is ``stop``. A reading that cannot be taken publishes a typed failure on its own topic and no
announcement: a registry then sees a live host it cannot read, which is not the same as a silent host. A failed publish is
logged and the next beat retries it, and ``start`` is not spent until one reaches the bus (the rule HostAdvertiser holds for
capacity advertisements, OMN-21811).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import yaml
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.errors import RuntimeHostError
from pydantic import BaseModel, ValidationError

from omnimarket.delegated_test_loop.lab_run_bus import ProtocolLabRunBus, event_type_for
from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.models.model_host_introspection_announcement import (
    EnumHostAnnouncementReason,
    ModelHostIntrospectionAnnouncement,
    ModelHostIntrospectionFailure,
)
from omnimarket.nodes.node_host_introspection_announce_effect.handlers.handler_host_introspection_gather import (
    HandlerHostIntrospectionGather,
)
from omnimarket.nodes.node_host_introspection_announce_effect.models.model_host_introspection_probe_request import (
    ModelHostIntrospectionProbeRequest,
)
from omnimarket.nodes.node_host_introspection_announce_effect.protocols.protocol_host_facts_reader import (
    HostFactsUnreadableError,
)
from omnimarket.nodes.node_host_introspection_compute.handlers.handler_host_introspection import (
    HostIntrospectionRefusedError,
)
from omnimarket.nodes.node_lab_work_unit_effect.protocols.local_host_reader import (
    HostCapacityUnreadableError,
)

logger = logging.getLogger(__name__)

CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contract.yaml"

#: What leaves a reading unreadable: the announcement is not made and the failure is published instead.
UNREADABLE = (
    HostFactsUnreadableError,
    HostCapacityUnreadableError,
    HostIntrospectionRefusedError,
    NodeOverlayError,
    ValidationError,
)


class ModelAnnounceTopics(BaseModel):
    """The contract's ``announce`` block."""

    announced_topic: str
    failed_topic: str


def load_announce_topics() -> ModelAnnounceTopics:
    """The topics, read from this node's contract and nowhere else."""
    return ModelAnnounceTopics.model_validate(
        yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))["announce"]
    )


class HostAnnouncer:
    """Announces one host on a bus."""

    def __init__(
        self,
        bus: ProtocolLabRunBus,
        gather: HandlerHostIntrospectionGather,
        host_name: str,
        cadence_seconds: int,
        *,
        topics: ModelAnnounceTopics | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._bus = bus
        self._gather = gather
        self._host = host_name
        self._cadence = cadence_seconds
        self._topics = topics or load_announce_topics()
        self._now = now
        self.started = False
        self.last_digest: str | None = None
        self.announced = 0
        self.failures = 0
        self.publish_failures = 0

    async def _publish(self, topic: str, payload: BaseModel) -> bool:
        envelope = ModelEventEnvelope[dict[str, object]](
            payload=payload.model_dump(mode="json"),
            correlation_id=uuid.uuid4(),
            event_type=event_type_for(topic),
        )
        body = json.dumps(envelope.model_dump(mode="json")).encode("utf-8")
        try:
            await self._bus.publish(topic, self._host.encode("utf-8"), body)
        except (RuntimeHostError, OSError, TimeoutError):
            self.publish_failures += 1
            logger.exception(
                "host %s: introspection publish to %s failed (%d so far), the next beat retries",
                self._host,
                topic,
                self.publish_failures,
            )
            return False
        return True

    async def announce_once(
        self, reason: EnumHostAnnouncementReason | None = None
    ) -> ModelHostIntrospectionAnnouncement | ModelHostIntrospectionFailure | None:
        """Read and publish once; answers what was published, or None when the publish itself failed."""
        wanted = reason or (
            EnumHostAnnouncementReason.HEARTBEAT
            if self.started
            else EnumHostAnnouncementReason.START
        )
        try:
            announcement = await asyncio.to_thread(
                self._gather.handle,
                ModelHostIntrospectionProbeRequest(host_name=self._host, reason=wanted),
            )
        except UNREADABLE as exc:
            self.failures += 1
            logger.warning("host %s: facts unreadable: %s", self._host, exc)
            failure = ModelHostIntrospectionFailure(
                host_name=self._host,
                failed_at=self._now(),
                reason=wanted,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            if await self._publish(self._topics.failed_topic, failure):
                return failure
            return None
        if (
            reason is None
            and self.started
            and announcement.facts_digest != self.last_digest
        ):
            announcement = announcement.model_copy(
                update={"reason": EnumHostAnnouncementReason.CHANGE}
            )
        if not await self._publish(self._topics.announced_topic, announcement):
            return None
        self.started = True
        self.last_digest = announcement.facts_digest
        self.announced += 1
        return announcement

    async def run(self, stop: asyncio.Event) -> None:
        """Announce until ``stop`` is set, then announce ``stop`` once."""
        while not stop.is_set():
            await self.announce_once()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self._cadence)
        await self.announce_once(EnumHostAnnouncementReason.STOP)


__all__ = [
    "UNREADABLE",
    "HostAnnouncer",
    "ModelAnnounceTopics",
    "load_announce_topics",
]
