# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HostAnnouncer -- publishes one host's announcements and keeps what it has announced (OMN-24367).

The announcer is called once per capacity beat with the capacity advertisement that beat published. It reads the
host's facts and decides the reason: ``start`` until a start has been published, ``facts_changed`` when the
facts digest differs from the last published one, ``heartbeat`` once ``heartbeat_seconds`` have passed since the
last publish, and nothing otherwise. ``stop`` is published on the way out.

Two promises, each with a test:

* A host whose facts cannot be read publishes a typed ``ModelHostIntrospectionFailure`` on the failure topic and
  no announcement; the next beat reads again.
* The start announcement is spent only by a successful publish. A failed publish leaves the announcer exactly
  as it was, so the next beat announces ``start`` again instead of a heartbeat that nobody has a start for.

The announcement's capacity is the advertisement it was handed, not a second reading, so the two agree field
for field.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from importlib import resources

import yaml
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.errors import RuntimeHostError
from pydantic import BaseModel, ConfigDict

from omnimarket.delegated_test_loop.lab_run_bus import ProtocolLabRunBus, event_type_for
from omnimarket.models.model_host_capacity_advertisement import (
    ModelHostCapacityAdvertisement,
)
from omnimarket.nodes.node_host_introspection_announce_effect.handlers.handler_host_facts_read_effect import (
    HandlerHostFactsReadEffect,
)
from omnimarket.nodes.node_host_introspection_announce_effect.models.model_host_introspection_announce import (
    ModelHostFactsProbeRequest,
    ModelHostIntrospectionFailure,
)
from omnimarket.nodes.node_host_introspection_compute.handlers.handler_host_introspection import (
    HandlerHostIntrospection,
    facts_digest,
)
from omnimarket.nodes.node_host_introspection_compute.models.model_host_announcement import (
    EnumAnnounceReason,
    ModelHostAnnouncement,
    ModelHostIntrospectionRequest,
)

logger = logging.getLogger(__name__)

ANNOUNCE_NODE = "node_host_introspection_announce_effect"


class ModelHostAnnounceTopics(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    announced: str
    failed: str
    heartbeat_seconds: int


def load_host_announce_topics(node: str = ANNOUNCE_NODE) -> ModelHostAnnounceTopics:
    """Read the announcement and failure topics and the heartbeat from the node contract, and nowhere else."""
    text = (
        resources.files(f"omnimarket.nodes.{node}")
        .joinpath("contract.yaml")
        .read_text()
    )
    contract = yaml.safe_load(text)
    announce = contract["announce"]
    return ModelHostAnnounceTopics(
        announced=announce["announced_topic"],
        failed=announce["failed_topic"],
        heartbeat_seconds=int(announce["heartbeat_seconds"]),
    )


class HostAnnouncer:
    """One host's announcer: reads facts, builds the announcement, publishes it, remembers what it published."""

    def __init__(
        self,
        bus: ProtocolLabRunBus,
        probe: ModelHostFactsProbeRequest,
        *,
        topics: ModelHostAnnounceTopics | None = None,
        facts_handler: HandlerHostFactsReadEffect | None = None,
        compute: HandlerHostIntrospection | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._bus = bus
        self._probe = probe
        self._topics = topics or load_host_announce_topics()
        self._facts = facts_handler or HandlerHostFactsReadEffect()
        self._compute = compute or HandlerHostIntrospection()
        self._now = now
        self._clock = clock
        #: True once a start announcement has been published; only a successful publish sets it.
        self.started = False
        self.last_digest = ""
        self.last_published = 0.0
        self.published = 0
        self.failures = 0

    @property
    def topics(self) -> ModelHostAnnounceTopics:
        return self._topics

    async def announce(
        self, capacity: ModelHostCapacityAdvertisement
    ) -> ModelHostAnnouncement | ModelHostIntrospectionFailure | None:
        """One beat: the announcement published, the failure published, or None when nothing is due."""
        return await self._beat(capacity, stopping=False)

    async def stop(
        self, capacity: ModelHostCapacityAdvertisement
    ) -> ModelHostAnnouncement | ModelHostIntrospectionFailure | None:
        """Publish the stop announcement; a host that never announced a start has nothing to stop."""
        if not self.started:
            return None
        return await self._beat(capacity, stopping=True)

    async def _beat(
        self, capacity: ModelHostCapacityAdvertisement, *, stopping: bool
    ) -> ModelHostAnnouncement | ModelHostIntrospectionFailure | None:
        try:
            facts = await asyncio.to_thread(self._facts.handle, self._probe)
        except Exception as exc:
            return await self._publish_failure(self._reason(None, stopping), exc)
        digest = facts_digest(facts)
        reason = self._reason(digest, stopping)
        if reason is EnumAnnounceReason.HEARTBEAT and not self._due():
            return None
        announcement = self._compute.handle(
            ModelHostIntrospectionRequest(
                facts=facts,
                capacity=capacity,
                reason=reason,
                announced_at=self._now(),
            )
        )
        if not await self._publish(self._topics.announced, announcement):
            return None
        self.published += 1
        self.last_published = self._clock()
        if reason is EnumAnnounceReason.STOP:
            self.started = False
        else:
            self.started = True
            self.last_digest = digest
        return announcement

    def _reason(self, digest: str | None, stopping: bool) -> EnumAnnounceReason:
        """Why this beat announces; with no digest (facts unread) the reason is the one the beat would have had."""
        if stopping:
            return EnumAnnounceReason.STOP
        if not self.started:
            return EnumAnnounceReason.START
        if digest is not None and digest != self.last_digest:
            return EnumAnnounceReason.FACTS_CHANGED
        return EnumAnnounceReason.HEARTBEAT

    def _due(self) -> bool:
        return self._clock() - self.last_published >= self._topics.heartbeat_seconds

    async def _publish_failure(
        self, reason: EnumAnnounceReason, exc: Exception
    ) -> ModelHostIntrospectionFailure:
        self.failures += 1
        failure = ModelHostIntrospectionFailure(
            host_name=self._probe.host_name,
            reason=reason,
            failed_at=self._now(),
            error_message=f"{type(exc).__name__}: {exc}"[:2000],
        )
        logger.error(
            "host %s: facts unreadable (%d so far), no announcement published: %s",
            self._probe.host_name,
            self.failures,
            exc,
        )
        await self._publish(self._topics.failed, failure)
        return failure

    async def _publish(self, topic: str, payload: BaseModel) -> bool:
        envelope = ModelEventEnvelope[dict[str, object]](
            payload=payload.model_dump(mode="json"),
            correlation_id=uuid.uuid4(),
            event_type=event_type_for(topic),
        )
        body = json.dumps(envelope.model_dump(mode="json")).encode("utf-8")
        try:
            await self._bus.publish(topic, self._probe.host_name.encode("utf-8"), body)
        except (RuntimeHostError, OSError, TimeoutError):
            logger.exception(
                "host %s: publish to %s failed; the next beat retries",
                self._probe.host_name,
                topic,
            )
            return False
        return True


__all__ = [
    "HostAnnouncer",
    "ModelHostAnnounceTopics",
    "load_host_announce_topics",
]
