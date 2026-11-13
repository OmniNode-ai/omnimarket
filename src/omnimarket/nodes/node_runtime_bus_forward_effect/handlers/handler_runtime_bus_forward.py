# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A run's recorded runtime-bus events carried to the hosted bus (OMN-24003, the runtime's after-run seam).

A runtime-local bus keeps the events a node run published in its own process, so without a forward they never
leave it and the hosted lane sees nothing of the run. The omnibase_core runtime (RuntimeLocal) calls the
forwarder registered under the ``onex.runtime_bus_forwarders`` entry-point group after the run ends, handing
over the recording; this handler is the piece of that forwarder that decides and counts. It dials nothing and
holds no setting of its own: every publish goes through the publisher injected at construction, which owns the
dial of the hosted bus the node's own overlay declares, and ``max_events``, ``flush_timeout_seconds`` and the
whole ``allowed_topic_prefixes`` allow-list are overlay values — an absent key refuses the overlay load before
this handler is ever built.

What it refuses, it refuses by name, never silently. A recording that holds more than ``max_events`` events is
carried not at all — nothing is published, every event is refused, and the receipt says why — because a
recording past the cap is trimmed by nobody. An event whose topic is under none of the allowed prefixes is
refused and never handed to the publisher. A publish the client refuses counts as failed with the client's own
words for the reason, and a flush that leaves events unconfirmed within ``flush_timeout_seconds`` subtracts
them from ``forwarded`` the same way: ``forwarded`` is exactly the events the flush confirmed delivered, so
``ok`` reads true exactly when the whole recording landed.

Canonical effect shape: ``handle(ModelRuntimeBusForwardCommand) -> ModelRuntimeBusForwardResult``.
"""

from __future__ import annotations

from omnimarket.nodes.node_runtime_bus_forward_effect.models import (
    ModelForwardRefusal,
    ModelRuntimeBusForwardCommand,
    ModelRuntimeBusForwardResult,
)
from omnimarket.nodes.node_runtime_bus_forward_effect.overlay import (
    ModelRuntimeBusForwardOverlay,
)
from omnimarket.nodes.node_runtime_bus_forward_effect.protocols import (
    ProtocolRuntimeBusPublisher,
    PublisherRefusedError,
)


class HandlerRuntimeBusForward:
    """Carry one run's recorded runtime-bus events to the hosted bus through the injected publisher."""

    def __init__(
        self,
        overlay: ModelRuntimeBusForwardOverlay,
        publisher: ProtocolRuntimeBusPublisher,
    ) -> None:
        self._overlay = overlay
        self._publisher = publisher

    def handle(
        self, request: ModelRuntimeBusForwardCommand
    ) -> ModelRuntimeBusForwardResult:
        events = request.events
        if len(events) > self._overlay.max_events:
            # A recording past the cap is carried not at all, never trimmed to it.
            return ModelRuntimeBusForwardResult(
                forwarded=0,
                failed=len(events),
                refusals=tuple(
                    ModelForwardRefusal(
                        topic=event.topic, reason="more than max_events"
                    )
                    for event in events
                ),
                detail="more than max_events",
            )
        refusals: list[ModelForwardRefusal] = []
        published = 0
        for event in events:
            if not any(
                event.topic.startswith(prefix)
                for prefix in self._overlay.allowed_topic_prefixes
            ):
                refusals.append(
                    ModelForwardRefusal(
                        topic=event.topic,
                        reason="topic outside allowed_topic_prefixes",
                    )
                )
                continue
            try:
                self._publisher.publish(
                    event.topic,
                    event.key_bytes(),
                    event.value_bytes(),
                    event.headers,
                )
            except PublisherRefusedError as exc:
                refusals.append(ModelForwardRefusal(topic=event.topic, reason=str(exc)))
                continue
            published += 1
        undelivered = min(
            self._publisher.flush(self._overlay.flush_timeout_seconds), published
        )
        detail = None
        if undelivered:
            detail = (
                f"{undelivered} event(s) not confirmed delivered "
                "within flush_timeout_seconds"
            )
            if reason := self._publisher.failure_detail():
                detail += f": {reason}"
        elif refusals:
            detail = f"{len(refusals)} event(s) refused: {refusals[0].reason}"
        return ModelRuntimeBusForwardResult(
            forwarded=published - undelivered,
            failed=len(refusals) + undelivered,
            refusals=tuple(refusals),
            detail=detail,
        )
