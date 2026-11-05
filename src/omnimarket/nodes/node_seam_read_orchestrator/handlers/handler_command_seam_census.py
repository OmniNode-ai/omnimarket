# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Standing census driven by the existing topic sampler, with bounded assembly."""

import asyncio
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from uuid import UUID, uuid4

from omnibase_core.models.dispatch.model_handler_output import ModelHandlerOutput

from omnimarket.events.topic_activity import ModelTopicActivitySampleEvent
from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.models.model_command_seam import (
    ModelCommandSeamFact,
    ModelCommandSeamFacts,
    ModelCommandSeamNode,
    ModelCommandSeamOverlay,
)
from omnimarket.nodes.node_seam_read_orchestrator.handlers.command_seam_inventory import (
    load_runtime_inventory,
    load_topic_last_crossings,
)

_NODE = "node_seam_read_orchestrator"


def _automatic_publishers(
    nodes: tuple[ModelCommandSeamNode, ...], roots: tuple[str, ...]
) -> dict[str, tuple[str, ...]]:
    """Trace publishers to a declared clock/hook ingress; a cycle is no root."""
    active_topics = set(roots)
    active_nodes: set[str] = set()
    changed = True
    while changed:
        changed = False
        for node in nodes:
            key = f"{node.package}/{node.node}"
            if key in active_nodes or node.binding_error:
                continue
            invocation = node.invocation
            external_clock = (
                invocation is not None
                and invocation.mode == "automatic"
                and invocation.trigger == "schedule_or_hook"
            )
            if external_clock or set(node.subscribes) & active_topics:
                active_nodes.add(key)
                active_topics.update(node.publishes)
                changed = True
    publishers: dict[str, list[str]] = {}
    for node in nodes:
        key = f"{node.package}/{node.node}"
        if key in active_nodes:
            for topic in node.publishes:
                publishers.setdefault(topic, []).append(key)
        invocation = node.invocation
        if (
            not node.binding_error
            and node.command_topic
            and invocation is not None
            and invocation.mode == "automatic"
            and invocation.trigger == "schedule_or_hook"
        ):
            publishers.setdefault(node.command_topic, []).append(invocation.sender)
    return {topic: tuple(sorted(set(names))) for topic, names in publishers.items()}


class HandlerCommandSeamCensus:
    """Sample event in, typed census facts out; runtime owns all publication."""

    def __init__(
        self,
        *,
        overlay: ModelCommandSeamOverlay | None = None,
        inventory: Callable[[], Awaitable[tuple[ModelCommandSeamNode, ...]]]
        | None = None,
        last_crossings: Callable[[], Awaitable[dict[str, datetime]]] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._overlay = overlay
        self._inventory = inventory
        self._last_crossings = last_crossings
        self._clock = clock or (lambda: datetime.now(UTC))
        self._parts: OrderedDict[str, dict[int, ModelTopicActivitySampleEvent]] = (
            OrderedDict()
        )
        self._completed: OrderedDict[str, None] = OrderedDict()
        self._nodes: tuple[ModelCommandSeamNode, ...] = ()
        self._inventory_at: datetime | None = None
        self._history: dict[str, datetime] = {}

    async def handle(
        self, request: ModelTopicActivitySampleEvent
    ) -> ModelHandlerOutput[None]:
        invocation_id = uuid4()
        now = self._clock()
        events: tuple[ModelCommandSeamFacts, ...] = ()
        try:
            overlay = self._overlay or load_structured_overlay(
                _NODE, ModelCommandSeamOverlay
            )
            self._overlay = overlay
            if request.part_count > 64:
                raise ValueError("sample-part-count-exceeds-bound")
            if request.sample_id in self._completed:
                return self._output(invocation_id, ())
            # Remove old incomplete samples; state is bounded even across lost parts.
            for key, parts in tuple(self._parts.items()):
                age = (now - next(iter(parts.values())).sampled_at).total_seconds()
                if age > overlay.max_age_seconds:
                    del self._parts[key]
            parts = self._parts.setdefault(request.sample_id, {})
            if parts:
                first = next(iter(parts.values()))
                if (first.sampled_at, first.part_count, first.broker_topics) != (
                    request.sampled_at,
                    request.part_count,
                    request.broker_topics,
                ):
                    raise ValueError("sample-part-metadata-conflict")
            prior = parts.get(request.part_index)
            if prior is not None and prior != request:
                raise ValueError("sample-part-content-conflict")
            parts[request.part_index] = request
            while len(self._parts) > 8:
                self._parts.popitem(last=False)
            if len(parts) != request.part_count:
                return self._output(invocation_id, ())
            samples = tuple(
                row for index in sorted(parts) for row in parts[index].topics
            )
            if len({row.topic for row in samples}) != len(samples):
                raise ValueError("sample-topic-duplicated")
            del self._parts[request.sample_id]
            if (
                self._inventory_at is None
                or (now - self._inventory_at).total_seconds()
                >= overlay.manifest_refresh_seconds
            ):
                self._nodes = await asyncio.wait_for(
                    self._inventory()
                    if self._inventory
                    else load_runtime_inventory(overlay),
                    timeout=overlay.request_timeout_seconds,
                )
                if self._last_crossings is not None or self._inventory is None:
                    self._history = await asyncio.wait_for(
                        self._last_crossings()
                        if self._last_crossings
                        else load_topic_last_crossings(overlay),
                        timeout=overlay.request_timeout_seconds,
                    )
                self._inventory_at = now
            automatic = _automatic_publishers(self._nodes, overlay.root_topics)
            by_topic = {row.topic: row for row in samples}
            broker_topics = set(request.broker_topics)
            facts: list[ModelCommandSeamFact] = []
            for node in self._nodes:
                for topic in node.subscribes:
                    if not topic.startswith("onex.cmd."):
                        continue
                    sample = by_topic.get(topic)
                    retained = sample.newest_message_at if sample else None
                    historical = self._history.get(topic)
                    last_crossed = retained
                    last_source = "sampler" if retained is not None else "unmeasured"
                    if historical is not None and (
                        retained is None or historical > retained
                    ):
                        last_crossed, last_source = historical, "projection_last_known"
                    facts.append(
                        ModelCommandSeamFact(
                            node=node.node,
                            package=node.package,
                            topic=topic,
                            invocation=node.invocation
                            if node.command_topic == topic
                            else None,
                            contract_senders=tuple(
                                sorted(
                                    f"{p.package}/{p.node}"
                                    for p in self._nodes
                                    if topic in p.publishes
                                )
                            ),
                            automatic_senders=automatic.get(topic, ()),
                            external_sender_declarations=tuple(
                                sorted(
                                    {
                                        p.external_senders[topic]
                                        for p in self._nodes
                                        if topic in p.external_senders
                                    }
                                )
                            ),
                            binding_error=node.binding_error,
                            topic_observed=topic in broker_topics,
                            high_watermark=sample.high_watermark_total
                            if sample
                            else None,
                            low_watermark=sample.low_watermark_total
                            if sample
                            else None,
                            last_crossed_at=last_crossed,
                            last_crossed_source=last_source,
                        )
                    )
            events = self._fact_events(request, tuple(facts))
            self._completed[request.sample_id] = None
            while len(self._completed) > 32:
                self._completed.popitem(last=False)
        except Exception as exc:
            # Error type only: endpoint strings and exception details can carry
            # deployment data. A failed read is a published UNKNOWN, never a zero.
            self._inventory_at = None
            failed = tuple(
                ModelCommandSeamFact(
                    node=n.node,
                    package=n.package,
                    topic=t,
                    invocation=n.invocation if n.command_topic == t else None,
                    binding_error="source-unreadable",
                    topic_observed=False,
                )
                for n in self._nodes
                for t in n.subscribes
                if t.startswith("onex.cmd.")
            )
            events = self._fact_events(request, failed, (type(exc).__name__,))
        return self._output(invocation_id, events)

    def _fact_events(
        self,
        request: ModelTopicActivitySampleEvent,
        facts: tuple[ModelCommandSeamFact, ...],
        errors: tuple[str, ...] = (),
    ) -> tuple[ModelCommandSeamFacts, ...]:
        overlay = self._overlay
        size = overlay.max_facts_per_event if overlay else 1
        count = max(1, (len(facts) + size - 1) // size)
        read_at = self._clock()
        return tuple(
            ModelCommandSeamFacts(
                scope=overlay.scope if overlay else "unconfigured",
                sample_id=request.sample_id,
                sampled_at=request.sampled_at,
                read_at=read_at,
                max_age_seconds=overlay.max_age_seconds
                if overlay
                else request.sample_interval_seconds,
                part_index=index,
                part_count=count,
                inventory_count=len(facts),
                facts=facts[index * size : (index + 1) * size],
                errors=errors,
            )
            for index in range(count)
        )

    @staticmethod
    def _output(
        invocation_id: UUID, events: tuple[ModelCommandSeamFacts, ...]
    ) -> ModelHandlerOutput[None]:
        return ModelHandlerOutput.for_orchestrator(
            input_envelope_id=invocation_id,
            correlation_id=invocation_id,
            handler_id=_NODE,
            events=events,
        )
