# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Derive the flow verdict from one heartbeat window. Pure; no I/O.

OMN-16777, Phase 1 of epic OMN-16776.

The runtime's heartbeat now carries a ``flow_window``: raw per-(consumer_group,
topic) counters for the window that just closed. This handler turns them into
the one fact nothing in the platform could previously state — whether a message
went IN and a message came OUT.

Canonical definition-B shape
----------------------------
``handle(request: ModelConsumerFlowProjectionRequest) ->
ModelConsumerFlowProjectionResult``: a typed payload in, a typed payload out, no
event envelope in the core, no ``_db``, no clock, no database. The facts the
derivation cannot know by itself — how much was produced upstream, and what
window this node last delivered — arrive AS INPUT, resolved by the writer that
owns the database. That is what keeps the derivation a pure function, so the
same request always produces the same rows (AC6), and it is why the write path
lives in ``ConsumerFlowProjectionWriter`` rather than here.

Why the verdict is computed here and not upstream
-------------------------------------------------
Envelope purity (doctrine gate 7): the producing event carries counters only. A
node that grades its own health can be wrong about itself in exactly the way
that hides an outage, which is what every green check on 2026-08-23 did.

The four states, and the fifth thing that is not a state
--------------------------------------------------------
``FLOWING`` / ``STALLED`` / ``STARVED`` / ``IDLE`` are verdicts. ``UNKNOWN`` is
not a verdict — it is the absence of an observation, materialized deliberately
so that a missing heartbeat cannot be read as a quiet one. Its counter fields
are ``None``, never 0 (AC5).
"""

from __future__ import annotations

from omnimarket.nodes.node_projection_consumer_flow.models import (
    EnumConsumerFlowState,
    EnumUpstreamEvidence,
    ModelConsumerFlowDeltaWire,
    ModelConsumerFlowProjectionRequest,
    ModelConsumerFlowProjectionResult,
    ModelConsumerFlowRow,
)

TABLE_FLOW = "consumer_flow_windows"
TABLE_PRODUCE = "topic_produce_windows"
FLOW_CONFLICT_KEY = "consumer_group,topic,window_start"
PRODUCE_CONFLICT_KEY = "topic,window_start"


def derive_flow_state(
    *,
    messages_in: int,
    messages_out: int,
    upstream_produced: int | None,
) -> tuple[EnumConsumerFlowState, EnumUpstreamEvidence]:
    """Classify one window's counters. Pure; no clock, no I/O, no ambient state.

    Args:
        messages_in: Envelopes the consumer was handed during the window.
        messages_out: Envelopes it successfully published as a result.
        upstream_produced: Envelopes the platform published TO this topic in an
            overlapping window, or ``None`` when the platform publishes there
            never — an externally-fed topic, about which this rail knows
            nothing.

    Returns:
        The verdict and the evidence class that produced it.

    The ``messages_in > 0`` branch does not consult upstream evidence at all,
    and that is deliberate: a consumer that took 15,750 messages and emitted
    zero is stalled whether or not anything else is producing. That is the
    OMN-16755 case, and it must not be rescued into green by a quiet upstream.
    """
    if messages_in > 0:
        state = (
            EnumConsumerFlowState.FLOWING
            if messages_out > 0
            else EnumConsumerFlowState.STALLED
        )
        evidence = (
            EnumUpstreamEvidence.PRODUCED
            if upstream_produced
            else (
                EnumUpstreamEvidence.NONE
                if upstream_produced is None
                else EnumUpstreamEvidence.SILENT
            )
        )
        return state, evidence

    if upstream_produced is None:
        # Nothing in this runtime publishes to the topic, so an external
        # producer is invisible here. Calling this STARVED would light up every
        # quiet externally-fed topic in the platform — the alert storm AC4
        # forbids. IDLE, and the row records WHY it is only IDLE.
        return EnumConsumerFlowState.IDLE, EnumUpstreamEvidence.NONE
    if upstream_produced > 0:
        return EnumConsumerFlowState.STARVED, EnumUpstreamEvidence.PRODUCED
    return EnumConsumerFlowState.IDLE, EnumUpstreamEvidence.SILENT


class HandlerProjectionConsumerFlow:
    """Derive consumer-flow rows from one heartbeat window."""

    def handle(
        self, request: ModelConsumerFlowProjectionRequest
    ) -> ModelConsumerFlowProjectionResult:
        """Canonical def-B entrypoint: typed payload in, typed payload out."""
        window = request.flow_window
        if window is None:
            # A heartbeat with no window is the normal priming / non-carrier
            # case. It is NOT a zero-traffic window, so it implies no rows at
            # all — absence, not zero.
            return ModelConsumerFlowProjectionResult()

        flow_rows = tuple(
            self._derive_row(
                delta,
                request.upstream_produced_by_topic.get(delta.topic),
            )
            for delta in window.consumer_deltas
        )
        return ModelConsumerFlowProjectionResult(
            flow_rows=flow_rows,
            produce_rows=window.produce_deltas,
            unknown_rows=self._unknown_rows(request),
        )

    @staticmethod
    def _derive_row(
        delta: ModelConsumerFlowDeltaWire,
        upstream: int | None,
    ) -> ModelConsumerFlowRow:
        state, evidence = derive_flow_state(
            messages_in=delta.messages_in,
            messages_out=delta.messages_out,
            upstream_produced=upstream,
        )
        return ModelConsumerFlowRow(
            consumer_group=delta.consumer_group,
            topic=delta.topic,
            window_start=delta.window_start,
            window_end=delta.window_end,
            node_id=delta.node_id,
            ingest_sequence=delta.window_sequence,
            messages_in=delta.messages_in,
            messages_out=delta.messages_out,
            messages_dlq=delta.messages_dlq,
            handler_errors=delta.handler_errors,
            upstream_produced=upstream,
            upstream_evidence=evidence,
            flow_state=state,
            evaluated_at=delta.window_end,
        )

    @staticmethod
    def _unknown_rows(
        request: ModelConsumerFlowProjectionRequest,
    ) -> tuple[ModelConsumerFlowRow, ...]:
        """Rows for windows this node never delivered.

        A heartbeat lost in transit takes its whole window with it. The gap is
        visible because ``window_sequence`` is monotonic per node: a jump from N
        to N+2 means window N+1 existed and was never seen. Those rows carry
        ``None`` counters. Writing nothing instead would leave the projection
        reporting the last known state, so a runtime that stops heartbeating
        entirely would read as permanently healthy — which is the failure this
        whole ticket is about.
        """
        window = request.flow_window
        last = request.last_observed_sequence
        if window is None or last is None or not request.known_keys:
            return ()
        if window.window_sequence <= last + 1:
            return ()
        return tuple(
            ModelConsumerFlowRow(
                consumer_group=group,
                topic=topic,
                # The lost window occupies the slot between the last observed
                # window and this one; the caller supplies that boundary as the
                # arriving window's own start.
                window_start=window.window_start,
                window_end=window.window_start,
                node_id=window.node_id,
                ingest_sequence=last + 1,
                messages_in=None,
                messages_out=None,
                messages_dlq=None,
                handler_errors=None,
                upstream_produced=None,
                upstream_evidence=EnumUpstreamEvidence.NONE,
                flow_state=EnumConsumerFlowState.UNKNOWN,
                evaluated_at=window.window_start,
            )
            for group, topic in request.known_keys
        )


__all__ = [
    "FLOW_CONFLICT_KEY",
    "PRODUCE_CONFLICT_KEY",
    "TABLE_FLOW",
    "TABLE_PRODUCE",
    "HandlerProjectionConsumerFlow",
    "derive_flow_state",
]
