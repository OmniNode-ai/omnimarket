# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Materialize per-consumer throughput windows and derive the flow verdict.

OMN-16777, Phase 1 of epic OMN-16776.

The runtime's heartbeat now carries a ``flow_window``: raw per-(consumer_group,
topic) counters for the window that just closed.  This projection turns those
counters into the one fact nothing in the platform could previously state —
whether a message went IN and a message came OUT — and materializes it as a row
per ``(consumer_group, topic, window_start)``.

Why the verdict is computed here and not upstream
-------------------------------------------------
Envelope purity (doctrine gate 7): the producing event carries counters only.  A
node that grades its own health can be wrong about itself in exactly the way
that hides an outage, which is what every green check on 2026-08-23 did.

The four states, and the fifth thing that is not a state
--------------------------------------------------------
``FLOWING`` / ``STALLED`` / ``STARVED`` / ``IDLE`` are verdicts.  ``UNKNOWN`` is
not a verdict — it is the absence of an observation, materialized deliberately
so that a missing heartbeat cannot be read as a quiet one.  Its counter columns
are NULL, never 0 (OMN-16777 AC5).

Ordering
--------
Rows are ordered by producer-assigned event time (``window_start``) with the
producer's ``window_sequence`` as the monotonic tie-breaker.  Never by an
ingest/`created_at` clock: a redelivery would then overwrite newer state with
older, which is how a projection quietly starts lying.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from omnimarket.nodes.node_projection_consumer_flow.models import (
    EnumConsumerFlowState,
    EnumUpstreamEvidence,
    ModelNodeFlowWindowWire,
)
from omnimarket.projection.protocol_database import DatabaseAdapter

logger = logging.getLogger(__name__)

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
        # forbids. IDLE, and the row says WHY it is only IDLE.
        return EnumConsumerFlowState.IDLE, EnumUpstreamEvidence.NONE
    if upstream_produced > 0:
        return EnumConsumerFlowState.STARVED, EnumUpstreamEvidence.PRODUCED
    return EnumConsumerFlowState.IDLE, EnumUpstreamEvidence.SILENT


def _iso(value: datetime) -> str:
    return value.isoformat()


def _as_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, (float, str)):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None
    return None


class HandlerProjectionConsumerFlow:
    """Project heartbeat flow windows into ``consumer_flow_windows``."""

    def handle(self, input_data: dict[str, object]) -> dict[str, object]:
        """RuntimeLocal projection-arm entrypoint.

        The runtime injects ``_db`` (a sync projection adapter) plus
        ``_``-prefixed transport keys; everything else is the heartbeat payload.
        """
        db_raw = input_data.pop("_db", None)
        if not isinstance(db_raw, DatabaseAdapter):
            raise TypeError("handle() requires a DatabaseAdapter in input_data['_db']")
        payload = {
            key: value
            for key, value in input_data.items()
            if not str(key).startswith("_")
        }
        raw_window = payload.get("flow_window")
        if raw_window is None:
            # A heartbeat with no window is the normal priming/non-carrier case.
            # It is NOT a zero-traffic window and must write nothing at all.
            return {"rows_upserted": 0}
        window = ModelNodeFlowWindowWire.model_validate(raw_window)
        return {"rows_upserted": self.project_window(window, db_raw)}

    def project_window(
        self, window: ModelNodeFlowWindowWire, db: DatabaseAdapter
    ) -> int:
        """Materialize one window. Returns the number of rows written."""
        written = 0
        # Produce tallies first: they are this window's upstream evidence, and a
        # consumer row derived before them would be classified on less
        # information than was available.
        written += self._project_produce_deltas(window, db)
        written += self._materialize_missing_windows(window, db)
        for delta in window.consumer_deltas:
            upstream = self._upstream_produced(
                db, delta.topic, delta.window_start, delta.window_end
            )
            state, evidence = derive_flow_state(
                messages_in=delta.messages_in,
                messages_out=delta.messages_out,
                upstream_produced=upstream,
            )
            row: dict[str, object] = {
                "consumer_group": delta.consumer_group,
                "topic": delta.topic,
                "window_start": _iso(delta.window_start),
                "window_end": _iso(delta.window_end),
                "node_id": str(delta.node_id),
                "ingest_sequence": delta.window_sequence,
                "messages_in": delta.messages_in,
                "messages_out": delta.messages_out,
                "messages_dlq": delta.messages_dlq,
                "handler_errors": delta.handler_errors,
                "upstream_produced": upstream,
                "upstream_evidence": evidence.value,
                "flow_state": state.value,
                # Event time, not wall clock: the row is a statement about the
                # window, so replaying the window reproduces it exactly (AC6).
                "evaluated_at": _iso(delta.window_end),
            }
            if self._is_stale_write(db, row):
                continue
            if db.upsert(TABLE_FLOW, FLOW_CONFLICT_KEY, row):
                written += 1
        return written

    def _project_produce_deltas(
        self, window: ModelNodeFlowWindowWire, db: DatabaseAdapter
    ) -> int:
        written = 0
        for produce in window.produce_deltas:
            row: dict[str, object] = {
                "topic": produce.topic,
                "window_start": _iso(produce.window_start),
                "window_end": _iso(produce.window_end),
                "node_id": str(produce.node_id),
                "ingest_sequence": produce.window_sequence,
                "messages_produced": produce.messages_produced,
                "evaluated_at": _iso(produce.window_end),
            }
            if db.upsert(TABLE_PRODUCE, PRODUCE_CONFLICT_KEY, row):
                written += 1
        return written

    def _upstream_produced(
        self,
        db: DatabaseAdapter,
        topic: str,
        window_start: datetime,
        window_end: datetime,
    ) -> int | None:
        """Sum production to ``topic`` over windows overlapping this one.

        ``None`` (not 0) when no producing window for the topic has ever been
        recorded: that is "we cannot see this topic's producers", which is a
        different fact from "its producers sent nothing", and the two must not
        collapse. The producer may be a different process on a different window
        boundary, so this OVERLAPS rather than matching ``window_start``
        exactly.
        """
        rows = db.query(TABLE_PRODUCE, {"topic": topic})
        if not rows:
            return None
        total = 0
        overlapped = False
        for row in rows:
            row_start = self._parse_ts(row.get("window_start"))
            row_end = self._parse_ts(row.get("window_end"))
            if row_start is None or row_end is None:
                continue
            if row_start < window_end and row_end > window_start:
                overlapped = True
                total += _as_int(row.get("messages_produced")) or 0
        return total if overlapped else None

    def _materialize_missing_windows(
        self, window: ModelNodeFlowWindowWire, db: DatabaseAdapter
    ) -> int:
        """Write UNKNOWN rows for windows this node never delivered.

        A heartbeat lost in transit takes its whole window with it. The gap is
        visible because ``window_sequence`` is monotonic per node: a jump from N
        to N+2 means window N+1 exists and was never seen. That interval is
        materialized with NULL counters and ``flow_state = UNKNOWN``.

        The alternative — writing nothing — leaves the projection reporting the
        last known state, so a runtime that stops heartbeating entirely reads as
        permanently healthy. That is the failure this whole ticket is about, so
        the silence has to become a row.
        """
        node_id = str(window.node_id)
        prior = db.query(TABLE_FLOW, {"node_id": node_id})
        if not prior:
            return 0
        sequences = [
            seq
            for seq in (_as_int(row.get("ingest_sequence")) for row in prior)
            if seq is not None
        ]
        if not sequences:
            return 0
        last_sequence = max(sequences)
        if window.window_sequence <= last_sequence + 1:
            return 0

        written = 0
        for row in prior:
            if _as_int(row.get("ingest_sequence")) != last_sequence:
                continue
            gap_start = row.get("window_end")
            if not isinstance(gap_start, str):
                continue
            unknown: dict[str, object] = {
                "consumer_group": row.get("consumer_group"),
                "topic": row.get("topic"),
                "window_start": gap_start,
                "window_end": _iso(window.window_start),
                "node_id": node_id,
                "ingest_sequence": last_sequence + 1,
                # NULL, not 0. `UNKNOWN != 0 messages` is the whole point.
                "messages_in": None,
                "messages_out": None,
                "messages_dlq": None,
                "handler_errors": None,
                "upstream_produced": None,
                "upstream_evidence": EnumUpstreamEvidence.NONE.value,
                "flow_state": EnumConsumerFlowState.UNKNOWN.value,
                "evaluated_at": _iso(window.window_start),
            }
            if self._would_overwrite_observed_window(db, unknown):
                continue
            if db.upsert(TABLE_FLOW, FLOW_CONFLICT_KEY, unknown):
                written += 1
        return written

    @staticmethod
    def _parse_ts(value: object) -> datetime | None:
        if isinstance(value, datetime):
            return value
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value)
            except ValueError:
                return None
        return None

    @staticmethod
    def _existing_row(
        db: DatabaseAdapter, row: dict[str, object]
    ) -> dict[str, Any] | None:
        matches = db.query(
            TABLE_FLOW,
            {
                "consumer_group": row["consumer_group"],
                "topic": row["topic"],
                "window_start": row["window_start"],
            },
        )
        return matches[0] if matches else None

    @classmethod
    def _is_stale_write(cls, db: DatabaseAdapter, row: dict[str, object]) -> bool:
        """True when a NEWER window for this key is already materialized.

        Redelivery is normal on an at-least-once bus. Without this guard a
        replayed older window overwrites a newer one and the projection reports
        state that has already been superseded. Sequences are only comparable
        within one node, so the guard applies only when the stored row came from
        the same node; a genuine cross-node collision on the same key writes
        last-wins and is recorded as a known limitation in the contract.
        """
        existing = cls._existing_row(db, row)
        if existing is None:
            return False
        if existing.get("node_id") != row.get("node_id"):
            return False
        stored = _as_int(existing.get("ingest_sequence"))
        incoming = _as_int(row.get("ingest_sequence"))
        if stored is None or incoming is None:
            return False
        return stored > incoming

    @classmethod
    def _would_overwrite_observed_window(
        cls, db: DatabaseAdapter, row: dict[str, object]
    ) -> bool:
        """Never downgrade a real observation to UNKNOWN.

        A late heartbeat can arrive after the gap has already been filled in.
        UNKNOWN is strictly less informative than any observed window, so it
        only ever writes into an empty slot.
        """
        return cls._existing_row(db, row) is not None


__all__ = [
    "FLOW_CONFLICT_KEY",
    "PRODUCE_CONFLICT_KEY",
    "TABLE_FLOW",
    "TABLE_PRODUCE",
    "HandlerProjectionConsumerFlow",
    "derive_flow_state",
]
