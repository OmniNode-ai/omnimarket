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
   [OMN-17862 SUPERSEDES THIS PARAGRAPH'S SECOND HALF.] This originally read
   "when the runtime injects ``_envelope_id`` the envelope UUID is used
   instead — a strictly stronger identity". It is not stronger, it is the
   WRONG identity: the envelope id is fresh on every Kafka redelivery, so one
   source event materialised N rows. The content address it fell back to was
   no better — the inbound model discarded the wire's per-event identifiers at
   parse, so two distinct tool executions hashed to one digest. Row identity is
   now the SOURCE EVENT's: the topic's own per-event id when the producer
   emits one, otherwise the required ``emitted_at`` with the topic and session
   id and that topic's distinguishing fields. See ``_source_event_material``.
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
import threading
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

# dlq-path-not-required: this handler never CATCHES a ValidationError -- it does
# not handle one at all. Its parse (`ModelSessionReplayEvent(**payload)`) raises
# one and lets it PROPAGATE to the runtime's projection dispatch callback, which
# is the surface that owns the DLQ route
# (`handler_wiring._route_projection_error_to_dlq`, reached through the
# `_is_projection_content_failure` allowlist that a pydantic ValidationError is
# already inside). Propagation is a durable signal, not a silent drop, which is
# the exact case the OMN-13548 gate's own escape hatch is documented for. A
# node-local DLQ route here would be a SECOND quarantine path racing the
# runtime's, and this node deliberately declares no `dlq_topics` so its refusals
# take the platform quarantine sink.
#
# Recorded rather than silently annotated: the gate matched a bare substring in
# the PROSE above -- the docstrings that explain why the refusal must be a
# ValidationError -- not in any code. The handler would have tripped it before
# this change for the same reason. That matcher breadth is a real (small) defect
# in `scripts/ci/check_projection_dlq_path.py`; it is noted on OMN-17862 rather
# than fixed here, because widening the blast radius of this change into a
# shared CI gate is not what this ticket is for.

TABLE = "session_replay_snapshots"
CONFLICT_KEY = "snapshot_id"

# [OMN-17862] How many times an ordinal allocation may be re-read after a peer
# writer PROVABLY took the ordinal this dispatch computed. Bounded because an
# unbounded loop against a genuinely stuck store is a hang, and re-raising is
# the correct exit: it withholds the offset and the record is redelivered.
# Never a substitute for the lock and the proof condition -- both of those run
# first, so reaching the bound at all means a real cross-process contention
# storm that an operator should see.
_ORDINAL_RETRY_ATTEMPTS = 3
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


# Topic -> the per-event identifier that topic's producer declares, when it
# declares one (OMN-17862). The three session-lifecycle topics have no per-event
# id by design; `emitted_at` is their discriminator alongside topic and session.
_TOPIC_SOURCE_EVENT_ID_FIELD: dict[str, str] = {
    TOPIC_PROMPT_SUBMITTED: "prompt_id",
    TOPIC_TOOL_EXECUTED: "tool_execution_id",
}


def _source_event_material(event: ModelSessionReplayEvent, topic: str) -> str:
    """The material that identifies ONE SOURCE EVENT, never one delivery.

    [OMN-17862] Two identities were on the table before this and both were
    wrong:

    * **The injected envelope id.** Fresh on every Kafka redelivery, so one
      source event materialised N rows -- this ticket's original symptom.
    * **The content address** (``_event_identity``: a hash of the whole
      ``model_dump``). The inbound model is ``extra="ignore"`` over fields none
      of which identified an event, and the two wire fields that DO identify a
      tool execution were being dropped at parse. Reproduced at the pinned
      commits: two genuinely distinct tool executions in one session produced
      the SAME dump and the SAME digest. Keying on it would have collapsed every
      ``tool_call`` of a session onto one row, each event silently overwriting
      the previous event's payload, ordinal and accumulated total -- the same
      silent-replacement harm the ``(session_id, sequence)`` conflict target is
      refused for, reached from the other direction.

    So the material is the SOURCE EVENT's own identity:

    1. the topic's per-event id when the producer emits one -- strictly the
       stronger discriminator, and used in preference; otherwise
    2. ``emitted_at`` (required, timezone-aware, normalized to UTC) together
       with that topic's own distinguishing fields.

    Branch 2 is not a lenient fallback, it is the branch the live lane runs.
    Measured read-only on the .201 stability lane 2026-09-07T23:2xZ, reading
    each stalled consumer group's committed offset forward over a frozen
    ``-o START:END`` range: **0 of 3000** ``tool-executed`` records carry a
    ``tool_execution_id`` and **3000 of 3000** carry a timezone-aware
    ``emitted_at``. Requiring the id would have quarantined the entire backlog
    this repair exists to drain.

    A delivery for which NEITHER can be established is never hashed into a
    projection that cannot tell two events apart -- and it cannot reach here,
    because ``emitted_at`` is required at parse and that refusal is a
    ``ValidationError`` the runtime's keep-or-ack allowlist already accepts.
    """
    id_field = _TOPIC_SOURCE_EVENT_ID_FIELD.get(topic)
    if id_field is not None:
        source_event_id = getattr(event, id_field)
        if source_event_id is not None:
            return f"{id_field}={source_event_id}"
    return json.dumps(
        {
            "emitted_at": _normalized_emitted_at(event),
            "distinguishing": _extract_state_delta(topic, event),
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _normalized_emitted_at(event: ModelSessionReplayEvent) -> str:
    """The stored/serialized form of the source timestamp: UTC ``isoformat()``.

    [OMN-17862] The two ends are different types -- ``emitted_at`` is an aware
    ``datetime`` on the wire, the stored column is a ``str`` that every pre-fix
    row holds in the ``+00:00`` form -- and ``timestamp`` is this exposure's
    declared ``freshness_column``. Left unpinned, a producer emitting a non-UTC
    offset (or a ``str(datetime)``-style space-separated rendering) would make
    the column textually heterogeneous against every existing row and the API
    would read the difference as staleness.

    ``astimezone(UTC)`` is safe here ONLY because the parse refuses a naive
    value: on a naive datetime ``astimezone`` assumes the HOST's local zone and
    converts.
    """
    return event.emitted_at.astimezone(UTC).isoformat()


def _derive_snapshot_id(
    *,
    session_id: str,
    topic: str,
    event: ModelSessionReplayEvent,
) -> str:
    """Derive the durable row identity for one event.

    [OMN-17862] The identity is the SOURCE EVENT's, never the delivery's. The
    ``envelope_id`` parameter is retained for the caller's snapshot-delta
    attribution and is deliberately NOT part of the material: it is fresh on
    every Kafka redelivery, so keying row identity on it is precisely the row
    inflation this ticket describes. What changes for the better is that a
    redelivery carrying a FRESH envelope id now finds its prior row too, not
    only one carrying the same envelope id.

    Deliberately NOT derived from ``(session_id, sequence)``: that is the
    OMN-17183 defect. ``sequence`` is reducer state, and keying row identity on
    reducer state means any failure to thread that state silently overwrites
    the whole session onto one row.
    """
    material = _source_event_material(event, topic)
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
        timestamp=_normalized_emitted_at(event),
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


def _rehydrate_state(latest: dict[str, object] | None) -> ModelSessionReplayState:
    """Rebuild reducer state from the session's SINGLE highest-ordinal row.

    The projection table is the durable state — the handler holds none between
    dispatches, and must not (the runtime may rebalance the partition onto a
    different consumer at any point). ``sequence`` continues from the highest
    stored ordinal rather than from a row count, so a gap left by an out-of-band
    delete cannot re-issue an ordinal that ``UNIQUE (session_id, sequence)``
    already holds.

    OMN-17888: this took ``list[dict]`` — every row of the session — and picked
    the maximum in Python. Reading n rows to compute one scalar is the whole
    O(n^2) defect; the caller now asks the store for that one row through
    ``ORDER BY sequence DESC LIMIT 1``, which
    ``idx_session_replay_session_sequence btree (session_id, sequence)`` answers
    with an index scan. The parameter is the row itself, or ``None`` for a
    session with no rows yet, so a caller CANNOT pass a full session and
    reintroduce the shape.
    """
    if latest is None:
        return ModelSessionReplayState()
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
        # [OMN-17862] Ordinal allocation is a check-then-act across two
        # connections, and the runtime dispatches five per-topic consume-loop
        # tasks into THIS ONE INSTANCE off-loop through `asyncio.to_thread`. The
        # locks are therefore real `threading` locks, not asyncio ones, and they
        # are interned per session id so unrelated sessions never contend.
        self._session_locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

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

        Pure: no I/O and NO CLOCK DEPENDENCE AT ALL. [OMN-17862] This sentence
        used to end "beyond the documented fallback for an event that carries no
        timestamp", and that fallback is retired. ``_build_row`` wrote
        ``event.timestamp or datetime.now(tz=UTC).isoformat()``; since no
        subscribed producer emits ``timestamp`` at all, the ``or`` fired on
        EVERY delivery and the wall clock supplied the stored value. Two
        deliveries of one event therefore stored two different timestamps under
        one identity, which a redelivery-dedup assertion on row count and
        identity alone passes straight through. The row's timestamp is now the
        source event's ``emitted_at``, normalized to UTC, and a delivery
        carrying none is refused at parse rather than clock-stamped -- a clock
        read is a fabricated default, and it makes a replayed projection
        non-deterministic.

        Args:
            state: Reducer state as of the event immediately before this one.
            event: Inbound session event.
            topic: Source topic string (determines event classification).
            snapshot_id: Pre-resolved row identity. When omitted it is derived
                from the SOURCE EVENT (``_source_event_material``), never from
                the delivery's envelope id or the lossy content address.

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

        Reducer state is rehydrated from the projection table on every call —
        the projection table is the only place it durably lives — but from ONE
        indexed row, never from the session.

        [OMN-17888] THE DEFECT THIS METHOD WAS REWRITTEN TO FIX. It opened with

            session_rows = db.query(TABLE, {"session_id": event.session_id})

        and then scanned that list twice in Python: once for the row matching
        the ``snapshot_id`` about to be written, once for the maximum
        ``sequence``. That is the ENTIRE session read back on EVERY event, so
        the cost of projecting a session is quadratic in its length, and the
        per-event cost of the busiest session grows without bound while nothing
        errors. Measured on the .201 dev lane 2026-09-07T15:53Z, session
        ``9787a4a3-ec49-4819-8bdc-5044efb94550`` held 100,441 of the table's
        103,468 rows and was growing ~3,029 rows/hour; each of its events was
        materialising ~100k rows into the runtime process, which is the
        allocation the OMN-17888 first pass measured at 225.4 MiB per call and
        the memcg-OOM-kill loop it produced. The 125,000-row budget that pass
        added would have refused that session outright around
        2026-09-08T00:00Z — the same defect, converted from an OOM into a stall.

        Both things this method needs are single-row INDEXED reads, and always
        were:

        * the row it is about to write, by ``snapshot_id`` — an equality lookup
          on ``session_replay_snapshots_pkey``. (Scoping it to the session was
          never load-bearing: ``snapshot_id`` is a digest OVER ``session_id``,
          so a global match on it is necessarily a match within the session.)
        * the session's newest row, by ``ORDER BY sequence DESC LIMIT 1`` --
          answered by ``idx_session_replay_session_sequence btree (session_id,
          sequence)``.

        Two reads per event, independent of session length, and the second one
        is skipped entirely on a redelivery. The ordering capability they use
        was added to the runtime read seam in the same change
        (``ProjectionDatabaseOperations.query``, omnibase_infra).

        Args:
            event: Inbound session event.
            db: Sync database adapter. The contract declares
                ``access: read_write`` because this method reads before it
                writes; ``access: write`` is refused fail-closed at the runtime
                read seam (OMN-16690).
            topic: Source topic string (determines event classification).
            envelope_id: The dispatched envelope's stable UUID when the runtime
                injected one. [OMN-17862] NO LONGER the row's identity -- it is
                fresh on every redelivery, which is this ticket's inflation. It
                is retained only as the republished snapshot delta's
                source-event attribution.

        Returns:
            Projection result with rows_upserted count.
        """
        snapshot_id = _derive_snapshot_id(
            session_id=event.session_id,
            topic=topic,
            event=event,
        )

        # [OMN-17862] The read-then-write below is a CHECK-THEN-ACT, and closing
        # its window is what drains the stall. `db.query` and `db.upsert` each
        # open and close their OWN connection with no transaction, no
        # `SELECT ... FOR UPDATE` and no lock spanning the two; the runtime runs
        # one consume-loop task PER TOPIC and this handler subscribes to five,
        # dispatching into ONE handler instance off-loop through
        # `asyncio.to_thread`. Two overlapping events of one session therefore
        # both read the same `max(sequence)` and both claim `max + 1`, and the
        # loser's insert collides on `UNIQUE (session_id, sequence)` -- a raw
        # store error the runtime cannot positively identify as the event's own
        # defect, so the offset is withheld and the partition rewinds forever.
        #
        # The per-session lock closes that window inside this process, which is
        # where the five loop tasks live. It is held across the ordinal read and
        # the write, and it is per SESSION rather than global so unrelated
        # sessions still project concurrently.
        with self._session_lock(event.session_id):
            return self._project_locked(event, db, topic, snapshot_id, envelope_id)

    def _session_lock(self, session_id: str) -> threading.Lock:
        """Return THE lock guarding this session's ordinal allocation.

        Locks are interned per session id under a short guard, so two threads
        asking for the same session get the SAME object. A fresh lock per call
        would be a lock that guards nothing -- which is the shape this method
        exists to make impossible to write by accident.
        """
        with self._locks_guard:
            lock = self._session_locks.get(session_id)
            if lock is None:
                lock = threading.Lock()
                self._session_locks[session_id] = lock
            return lock

    def _project_locked(
        self,
        event: ModelSessionReplayEvent,
        db: DatabaseAdapter,
        topic: str,
        snapshot_id: str,
        envelope_id: str | None,
    ) -> ModelProjectionReplayResult:
        """Read the ordinal and write the row with this session's lock held.

        The bounded re-read below covers the window the lock cannot: a SECOND
        RUNTIME PROCESS. An in-process lock is not a distributed one, so a peer
        container projecting the same session can still take `max + 1` between
        this process's read and its write.

        It re-reads the ordinal and retries ONLY ON PROOF that a peer moved it.
        Anything else -- a dead connection, a revoked grant, a missing relation
        -- re-raises the ORIGINAL exception unchanged, so a genuine write-path
        failure still withholds the offset exactly as it does today. Proof
        rather than an exception-type match is deliberate: matching on a driver
        class would special-case one library and silently swallow whatever the
        next store raises.

        This is not a retry ceiling standing in for the guard (which the plan
        forbids). It is the "let a genuine collision re-read `max` and retry
        rather than escaping as a raw `UniqueViolation`" half of the repair, and
        on exhaustion it RE-RAISES rather than acking -- so the failure mode of
        the retry is the loud stall, never a silent drop.
        """
        attempts = 0
        while True:
            attempts += 1
            prior_rows = db.query(TABLE, {CONFLICT_KEY: snapshot_id}, limit=1)
            prior = prior_rows[0] if prior_rows else None
            latest_ordinal: int | None = None

            if prior is not None:
                # Redelivery of an event already materialized. Re-derive the row
                # at its STORED ordinal and STORED total so the write is
                # byte-identical and the token count is not double-applied. The
                # write is repeated rather than skipped because the runtime
                # gates the terminal `projected` event on rows_upserted >= 1 and
                # logs an error at zero (handler_wiring, OMN-13360).
                row = _build_row(
                    event=event,
                    topic=topic,
                    sequence=_int_value(prior.get("sequence")),
                    cumulative_tokens=_int_value(prior.get("cumulative_tokens")),
                    snapshot_id=snapshot_id,
                )
            else:
                # Only reached when this event has NOT been materialised before,
                # so the second read is skipped on every redelivery -- the case
                # the branch above already answered from its own single-row
                # lookup.
                latest_rows = db.query(
                    TABLE,
                    {"session_id": event.session_id},
                    order_by="sequence",
                    descending=True,
                    limit=1,
                )
                latest = latest_rows[0] if latest_rows else None
                latest_ordinal = (
                    None if latest is None else _int_value(latest.get("sequence"))
                )
                _, row = self.accumulate(
                    _rehydrate_state(latest),
                    event,
                    topic,
                    snapshot_id=snapshot_id,
                )

            try:
                ok = db.upsert(TABLE, CONFLICT_KEY, _row_to_dict(row))
            except Exception as exc:
                if prior is not None or attempts > _ORDINAL_RETRY_ATTEMPTS:
                    raise
                recheck = db.query(
                    TABLE,
                    {"session_id": event.session_id},
                    order_by="sequence",
                    descending=True,
                    limit=1,
                )
                observed = _int_value(recheck[0].get("sequence")) if recheck else None
                if observed is None or observed == latest_ordinal:
                    # No peer moved the ordinal, so this was not a collision.
                    raise
                logger.warning(
                    "Session-replay ordinal collision on session=%s topic=%s "
                    "(attempt %d): a concurrent writer advanced the highest "
                    "stored sequence from %s to %s while this dispatch held %s; "
                    "re-reading the ordinal (OMN-17862)",
                    event.session_id,
                    topic,
                    attempts,
                    latest_ordinal,
                    observed,
                    row.sequence,
                    exc_info=exc,
                )
                continue

            if not ok:
                return ModelProjectionReplayResult(rows_upserted=0)
            break
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
