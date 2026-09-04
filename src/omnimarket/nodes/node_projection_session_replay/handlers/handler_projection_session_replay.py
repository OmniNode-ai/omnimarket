# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""HandlerProjectionSessionReplay — reduce session events into replay snapshots.

Consumes session lifecycle events from the five contract-declared subscribe
topics and maps them to replay event types. Each event produces one row in
``session_replay_snapshots``, carrying a per-session ``sequence`` ordinal and a
running ``cumulative_tokens`` total.

[OMN-13087] original node.

[OMN-17183] THE DEFECT THIS FILE WAS REWRITTEN TO FIX — silent data
destruction, proven live on the .201 stability lane 2026-08-30.

``handle()`` called ``project(event, db, topic)`` with ``state=`` omitted, and
``project()`` did ``current_state = state or ModelSessionReplayState()`` on
EVERY message. Nothing carried reducer state across dispatches, so:

* ``sequence`` was permanently 0;
* ``_derive_snapshot_id`` hashed ``f"{session_id}::{sequence}"``, so every
  event of a session derived the SAME ``snapshot_id``;
* ``CONFLICT_KEY = "snapshot_id"``, so each event UPSERTed over the previous.

Live result: 69,014 consumed ``tool-executed`` events materialized 15 rows —
one per session — with ``cumulative_tokens`` stuck at 0, consumer lag 0 and
DLQ 0 throughout. Nothing errored; the projection just ate the stream.

THE FIX, in the two shapes this repo already uses for the same problem:

1. **Row identity is content-addressed, never sequence-derived.** The sibling
   ``node_projection_work_events`` (OMN-16180) was built naming this exact
   defect: "a content-addressed key cannot degrade that way, and needs no
   cross-dispatch state to be correct" (``model_work_event.derive_event_id``).
   When the runtime injects ``_envelope_id`` the envelope UUID is used instead
   — a strictly stronger identity, and the reason ``handler_shim`` surfaces
   that key at all.
2. **Reducer state is rehydrated from the projection table, not from a fresh
   default.** ``sequence`` and ``cumulative_tokens`` are read back from the
   session's existing rows before each reduction, matching
   ``node_projection_event_chain``, ``node_projection_traces`` and
   ``node_projection_voice_sessions``. The contract therefore declares
   ``access: read_write`` — the runtime read seam
   (``ProjectionTableOperation._assert_read_declared``) refuses a read under
   ``access: write`` fail-closed (OMN-16690).

``project()`` no longer takes a ``state`` parameter. An optional argument that
silently defaults to "start over" is the footgun that produced this incident;
callers reducing in memory use the pure ``accumulate()`` instead.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import yaml

from omnimarket.nodes.contract_topics import contract_subscribe_topics
from omnimarket.nodes.node_projection_session_replay.models.model_session_replay import (
    ModelProjectionReplayResult,
    ModelReplaySnapshotRow,
    ModelSessionReplayEvent,
    ModelSessionReplayState,
)
from omnimarket.projection.discovery import load_projection_exposures_from_contract
from omnimarket.projection.handler_shim import (
    INJECTED_ENVELOPE_ID_KEY,
    INJECTED_TOPIC_KEY,
    split_projection_input,
)
from omnimarket.projection.models import ProjectionTableConfig
from omnimarket.projection.protocol_database import DatabaseAdapter
from omnimarket.projection.snapshot_publisher import (
    KafkaSnapshotDeltaPublisher,
    ProtocolSnapshotDeltaPublisher,
    encode_snapshot_delta,
    resolve_snapshot_bootstrap_servers,
)

logger = logging.getLogger(__name__)

TABLE = "session_replay_snapshots"
CONFLICT_KEY = "snapshot_id"
_DEFAULT_CONTRACT_PATH = Path(__file__).resolve().parent.parent / "contract.yaml"


@dataclass(frozen=True)
class _SessionReplayTopics:
    session_started: str
    prompt_submitted: str
    tool_executed: str
    session_outcome: str
    session_ended: str


def _load_topics(contract_path: Path | None = None) -> _SessionReplayTopics:
    topics = contract_subscribe_topics(contract_path or _DEFAULT_CONTRACT_PATH)
    if len(topics) != 5:
        raise ValueError(
            "node_projection_session_replay contract must declare exactly "
            f"five subscribe topics; found {len(topics)}."
        )
    return _SessionReplayTopics(
        session_started=topics[0],
        prompt_submitted=topics[1],
        tool_executed=topics[2],
        session_outcome=topics[3],
        session_ended=topics[4],
    )


_TOPICS = _load_topics()
TOPIC_SESSION_STARTED = _TOPICS.session_started
TOPIC_PROMPT_SUBMITTED = _TOPICS.prompt_submitted
TOPIC_TOOL_EXECUTED = _TOPICS.tool_executed
TOPIC_SESSION_OUTCOME = _TOPICS.session_outcome
TOPIC_SESSION_ENDED = _TOPICS.session_ended

# Topic → (event_type, node_name, is_checkpoint)
_TOPIC_MAP: dict[str, tuple[str, str, bool]] = {
    TOPIC_SESSION_STARTED: ("session_start", "session", True),
    TOPIC_PROMPT_SUBMITTED: ("user_input", "user", False),
    TOPIC_TOOL_EXECUTED: ("tool_call", "", False),
    TOPIC_SESSION_OUTCOME: ("checkpoint", "session", True),
    TOPIC_SESSION_ENDED: ("session_end", "session", True),
}


def _as_uuid_shape(digest: str) -> str:
    """Format a hex digest as a UUID-shaped string the dashboard expects."""
    return (
        f"{digest[:8]}-{digest[8:12]}-{digest[12:16]}-{digest[16:20]}-{digest[20:32]}"
    )


def _event_identity(event: ModelSessionReplayEvent, topic: str) -> str:
    """Canonical, order-stable serialization of one inbound event.

    ``sort_keys=True`` plus explicit separators make the form stable across
    Python versions and dict insertion order, exactly as
    ``node_projection_work_events.derive_event_id`` requires for the same
    reason.
    """
    return json.dumps(
        {"topic": topic, "event": event.model_dump(mode="json")},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _derive_snapshot_id(
    *,
    session_id: str,
    topic: str,
    event: ModelSessionReplayEvent,
    envelope_id: str | None = None,
) -> str:
    """Derive the durable row identity for one event.

    The identity is the envelope's stable UUID when the runtime injected one
    (``handler_shim.INJECTED_ENVELOPE_ID_KEY``), otherwise the content address
    of the event itself. Both are independent of any cross-dispatch counter, so
    a redelivery UPSERTs onto the same row instead of appending, and two
    genuinely distinct events never collide.

    Deliberately NOT derived from ``(session_id, sequence)``: that is the
    OMN-17183 defect. ``sequence`` is reducer state, and keying row identity on
    reducer state means any failure to thread that state silently overwrites
    the whole session onto one row.
    """
    material = envelope_id if envelope_id is not None else _event_identity(event, topic)
    digest = hashlib.sha256(
        "\x00".join((session_id, topic, material)).encode("utf-8")
    ).hexdigest()
    return _as_uuid_shape(digest)


def _classify_event(
    topic: str,
    event: ModelSessionReplayEvent,
) -> tuple[str, str, bool]:
    """Return (event_type, node_name, is_checkpoint) for the given topic."""
    if topic in _TOPIC_MAP:
        event_type, node_name, is_checkpoint = _TOPIC_MAP[topic]
        # For tool_call events, node_name is the tool's own name when available.
        if event_type == "tool_call" and event.tool_name:
            node_name = event.tool_name
        return event_type, node_name, is_checkpoint
    # Unknown topic: emit a generic event rather than silently drop.
    return "event", "unknown", False


def _extract_state_delta(
    topic: str,
    event: ModelSessionReplayEvent,
) -> dict[str, object]:
    """Extract a minimal state delta for the event type."""
    if topic == TOPIC_SESSION_STARTED:
        return {"session_id": event.session_id}
    if topic == TOPIC_PROMPT_SUBMITTED:
        delta: dict[str, object] = {}
        if event.prompt_preview is not None:
            delta["prompt_preview"] = event.prompt_preview
        if event.prompt_length is not None:
            delta["prompt_length"] = event.prompt_length
        return delta
    if topic == TOPIC_TOOL_EXECUTED:
        delta2: dict[str, object] = {}
        if event.tool_name is not None:
            delta2["tool_name"] = event.tool_name
        if event.tool_input is not None:
            delta2["tool_input"] = event.tool_input
        return delta2
    if topic == TOPIC_SESSION_OUTCOME:
        return {"outcome": event.outcome or "unknown"}
    if topic == TOPIC_SESSION_ENDED:
        return {"session_id": event.session_id}
    return {}


def _int_value(value: object) -> int:
    """Coerce a stored column value to int, defaulting to 0."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, (str, float)):
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0
    return 0


def _build_row(
    *,
    event: ModelSessionReplayEvent,
    topic: str,
    sequence: int,
    cumulative_tokens: int,
    snapshot_id: str,
) -> ModelReplaySnapshotRow:
    """Assemble one snapshot row from an event plus already-resolved state."""
    event_type, node_name, is_checkpoint = _classify_event(topic, event)
    return ModelReplaySnapshotRow(
        snapshot_id=snapshot_id,
        session_id=event.session_id,
        sequence=sequence,
        timestamp=event.timestamp or datetime.now(tz=UTC).isoformat(),
        event_type=event_type,
        node_name=node_name,
        state_delta=_extract_state_delta(topic, event),
        cumulative_tokens=cumulative_tokens,
        is_checkpoint=is_checkpoint,
    )


def _row_to_dict(row: ModelReplaySnapshotRow) -> dict[str, object]:
    """Column mapping for the UPSERT, matching migration 0001's shape."""
    return {
        "snapshot_id": row.snapshot_id,
        "session_id": row.session_id,
        "sequence": row.sequence,
        "timestamp": row.timestamp,
        "event_type": row.event_type,
        "node_name": row.node_name,
        "state_delta": row.state_delta,
        "cumulative_tokens": row.cumulative_tokens,
        "is_checkpoint": row.is_checkpoint,
    }


def _rehydrate_state(rows: list[dict[str, object]]) -> ModelSessionReplayState:
    """Rebuild reducer state from the session's already-materialized rows.

    The projection table is the durable state — the handler holds none between
    dispatches, and must not (the runtime may rebalance the partition onto a
    different consumer at any point). ``sequence`` continues from the highest
    stored ordinal rather than ``len(rows)`` so a gap left by an out-of-band
    delete cannot re-issue an ordinal that ``UNIQUE (session_id, sequence)``
    already holds.
    """
    if not rows:
        return ModelSessionReplayState()
    latest = max(rows, key=lambda row: _int_value(row.get("sequence")))
    return ModelSessionReplayState(
        sequence=_int_value(latest.get("sequence")) + 1,
        cumulative_tokens=_int_value(latest.get("cumulative_tokens")),
    )


class HandlerProjectionSessionReplay:
    """Reducer: accumulate session events into replay snapshot rows.

    [OMN-17774] Every row this reducer durably writes is also republished as a
    keyed snapshot delta onto the exposure's own topic. That republish is the
    ONLY way the row reaches a reader: the projection API process holds no
    database handle by design (OMN-15800 seam B), so an exposure becomes visible
    exactly when — and only when — its writer publishes.

    Whether anything is published is entirely contract-driven.
    ``encode_snapshot_delta`` returns ``None`` for an exposure that does not
    declare ``bus_backed``, so the call below is unconditional and the contract
    alone decides. That is the ordering rule the epic makes explicit: the flag
    and its writer land together, because a flag ahead of its writer converts an
    honest refusal into a confident empty.
    """

    def __init__(
        self,
        *,
        contract_path: Path | None = None,
        publisher: ProtocolSnapshotDeltaPublisher | None = None,
    ) -> None:
        """Load this node's own exposure and bind the republish transport.

        Args:
            contract_path: Override for the node's ``contract.yaml``. Defaults
                to the shipped one beside this package.
            publisher: Transport for encoded snapshot deltas. Injected by tests
                and by any caller that wants to own the lifecycle; otherwise a
                per-call Kafka producer is built lazily on first publish, so
                constructing this handler touches no broker and reads no
                settings.
        """
        path = contract_path or _DEFAULT_CONTRACT_PATH
        with open(path) as handle:
            contract: dict[str, object] = yaml.safe_load(handle)
        exposures = load_projection_exposures_from_contract(
            contract, str(contract.get("name", "projection_session_replay")), path
        )
        self._snapshot_exposure: ProjectionTableConfig | None = next(
            (exposure for exposure in exposures if exposure.bus_backed), None
        )
        self._publisher: ProtocolSnapshotDeltaPublisher | None = publisher

    def _resolve_publisher(self) -> ProtocolSnapshotDeltaPublisher:
        """Return the bound publisher, building the default one once.

        Built lazily rather than in ``__init__``: the runtime constructs every
        projection handler at wiring time, including in processes and tests that
        never publish, and resolving broker settings there would make handler
        construction depend on transport configuration it may not need.
        """
        if self._publisher is None:
            self._publisher = KafkaSnapshotDeltaPublisher(
                bootstrap_servers=resolve_snapshot_bootstrap_servers()
            )
        return self._publisher

    def _publish_snapshot(
        self,
        row: ModelReplaySnapshotRow,
        *,
        source_topic: str,
        source_event_id: str,
    ) -> bool:
        """Republish one materialized row as a keyed snapshot delta.

        The ordering coordinates are fixed at partition 0 / offset 0, and that
        is a decision, not an omission. The runtime's projection dispatch seam
        injects only ``_db``/``_event_type``/``_topic``/``_envelope_id``
        (``handler_shim.RUNTIME_INJECTED_KEYS``) — a sync projection handler
        never sees the source message's Kafka coordinates, so there is no real
        offset to pass and inventing a monotonic counter here would be a
        process-local token of exactly the kind OMN-15800 round 3 removed.

        Fixed coordinates are CORRECT for this exposure because its key is
        ``snapshot_id``, which is content-addressed per source event: one source
        event owns exactly one key. ``SnapshotCache.apply_message`` therefore
        only ever compares a key against a delta derived from the SAME source
        event — a Kafka redelivery — and dropping that as a replay is the
        intended idempotence, not lost data. It would be wrong for a mutable
        key grain, which is why
        ``test_every_distinct_source_event_owns_its_own_key`` asserts the
        premise rather than trusting it.
        """
        exposure = self._snapshot_exposure
        if exposure is None:
            return False
        message = encode_snapshot_delta(
            exposure,
            op="upsert",
            row=_row_to_dict(row),
            source_event_id=source_event_id,
            source_topic=source_topic,
            source_partition=0,
            source_offset=0,
            observed_at=datetime.now(tz=UTC).isoformat(),
        )
        if message is None:
            return False
        return self._resolve_publisher().publish(message)

    def accumulate(
        self,
        state: ModelSessionReplayState,
        event: ModelSessionReplayEvent,
        topic: str,
        *,
        snapshot_id: str | None = None,
    ) -> tuple[ModelSessionReplayState, ModelReplaySnapshotRow]:
        """Reduce one event into a snapshot row and advance state.

        Pure: no I/O, no clock dependence beyond the documented fallback for an
        event that carries no timestamp.

        Args:
            state: Reducer state as of the event immediately before this one.
            event: Inbound session event.
            topic: Source topic string (determines event classification).
            snapshot_id: Pre-resolved row identity. When omitted it is derived
                from the event's content address.

        Returns:
            Updated state and the new snapshot row to persist.
        """
        cumulative_tokens = state.cumulative_tokens + (event.tokens_used or 0)
        row = _build_row(
            event=event,
            topic=topic,
            sequence=state.sequence,
            cumulative_tokens=cumulative_tokens,
            snapshot_id=snapshot_id
            or _derive_snapshot_id(
                session_id=event.session_id, topic=topic, event=event
            ),
        )
        new_state = ModelSessionReplayState(
            sequence=state.sequence + 1,
            cumulative_tokens=cumulative_tokens,
        )
        return new_state, row

    def project(
        self,
        event: ModelSessionReplayEvent,
        db: DatabaseAdapter,
        topic: str,
        envelope_id: str | None = None,
    ) -> ModelProjectionReplayResult:
        """Project one event to the session_replay_snapshots table.

        Reducer state is rehydrated from the session's existing rows on every
        call — the projection table is the only place it durably lives.

        Args:
            event: Inbound session event.
            db: Sync database adapter. The contract declares
                ``access: read_write`` because this method reads before it
                writes; ``access: write`` is refused fail-closed at the runtime
                read seam (OMN-16690).
            topic: Source topic string (determines event classification).
            envelope_id: The dispatched envelope's stable UUID when the runtime
                injected one, used as the durable idempotency key.

        Returns:
            Projection result with rows_upserted count.
        """
        snapshot_id = _derive_snapshot_id(
            session_id=event.session_id,
            topic=topic,
            event=event,
            envelope_id=envelope_id,
        )
        session_rows = db.query(TABLE, {"session_id": event.session_id})
        prior = next(
            (r for r in session_rows if r.get(CONFLICT_KEY) == snapshot_id),
            None,
        )

        if prior is not None:
            # Redelivery of an event already materialized. Re-derive the row at
            # its STORED ordinal and STORED total so the write is byte-identical
            # and the token count is not double-applied. The write is repeated
            # rather than skipped because the runtime gates the terminal
            # `projected` event on rows_upserted >= 1 and logs an error at zero
            # (handler_wiring, OMN-13360).
            row = _build_row(
                event=event,
                topic=topic,
                sequence=_int_value(prior.get("sequence")),
                cumulative_tokens=_int_value(prior.get("cumulative_tokens")),
                snapshot_id=snapshot_id,
            )
        else:
            _, row = self.accumulate(
                _rehydrate_state(session_rows),
                event,
                topic,
                snapshot_id=snapshot_id,
            )

        ok = db.upsert(TABLE, CONFLICT_KEY, _row_to_dict(row))
        if not ok:
            return ModelProjectionReplayResult(rows_upserted=0)
        published = self._publish_snapshot(
            row,
            source_topic=topic,
            source_event_id=envelope_id if envelope_id is not None else snapshot_id,
        )
        return ModelProjectionReplayResult(
            rows_upserted=1, snapshot_published=published
        )

    def handle(self, input_data: dict[str, object]) -> dict[str, object]:
        """RuntimeLocal handler protocol shim.

        Runtime-injected bookkeeping keys are stripped through the canonical
        ``split_projection_input`` seam rather than hand-rolled ``pop`` calls —
        the pattern that drifted in OMN-16249 when ``_envelope_id`` was added
        upstream.
        """
        db, payload, injected = split_projection_input(input_data)
        topic = str(injected.get(INJECTED_TOPIC_KEY) or TOPIC_SESSION_STARTED)
        raw_envelope_id = injected.get(INJECTED_ENVELOPE_ID_KEY)
        envelope_id = None if raw_envelope_id is None else str(raw_envelope_id)
        event = ModelSessionReplayEvent(**payload)
        result = self.project(event, db, topic, envelope_id=envelope_id)
        return result.model_dump(mode="json")


__all__: list[str] = ["HandlerProjectionSessionReplay"]
