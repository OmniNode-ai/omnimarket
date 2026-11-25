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

import json
import os
from pathlib import Path

from omnimarket.nodes.node_runtime_bus_forward_effect.models import (
    ModelForwardEvent,
    ModelForwardRefusal,
    ModelRuntimeBusForwardCommand,
    ModelRuntimeBusForwardResult,
    ModelRuntimeBusLocalRecordCommand,
    ModelRuntimeBusLocalRecordResult,
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


# A machine whose operator declared no hosted bus (no overlay for this node) keeps a run's recorded events on disk
# (OMN-26451): the receipt says they were kept locally and not delivered to a hosted bus. A write that fails raises
# ``OSError``, which the caller reports as a degraded reading, never as a delivery. A dry run writes nothing, so a
# recording of a dry run's events is not kept either.
LOCAL_RECORD_FILE_NAME = "runtime_bus_events.jsonl"
_DRY_RUN_MODES = frozenset({"dry-run", "dry_run"})


def _event_records_a_dry_run(event: ModelForwardEvent) -> bool:
    """True when the event's JSON object value declares a dry run: ``dry_run`` true, or ``mode`` ``dry-run``/``dry_run``."""
    try:
        value = json.loads(event.value_bytes())
    except ValueError:
        return False
    if not isinstance(value, dict):
        return False
    return value.get("dry_run") is True or value.get("mode") in _DRY_RUN_MODES


class HandlerRuntimeBusLocalRecord:
    """Appends recorded events to the local record file and answers how many it kept."""

    def __init__(self, *, record_dir: Path) -> None:
        self._record_dir = record_dir

    @property
    def path(self) -> Path:
        return self._record_dir / LOCAL_RECORD_FILE_NAME

    def handle(
        self, request: ModelRuntimeBusLocalRecordCommand
    ) -> ModelRuntimeBusLocalRecordResult:
        """Append the request's events in order and answer the count kept; ``OSError`` when the file cannot be written.

        A request that holds any event of a dry run keeps nothing and touches no file: a dry run writes no local record.
        """
        if any(_event_records_a_dry_run(event) for event in request.events):
            return ModelRuntimeBusLocalRecordResult(kept=0, dry_run=True)
        self._record_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        lines = "".join(
            json.dumps(event.model_dump(mode="json"), sort_keys=True) + "\n"
            for event in request.events
        )
        descriptor = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(descriptor, "a", encoding="utf-8") as record:
            record.write(lines)
        return ModelRuntimeBusLocalRecordResult(
            kept=len(request.events), record_path=str(self.path)
        )
