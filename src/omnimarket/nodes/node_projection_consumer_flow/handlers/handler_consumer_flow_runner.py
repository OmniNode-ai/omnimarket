# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Standalone projection runner for consumer flow windows (OMN-16777).

Consumes ``onex.evt.platform.node-heartbeat.v1`` — the topic the runtime already
emits on — and writes the flow rows the read model serves, then publishes
snapshot deltas on first sighting, verdict change, or a bounded refresh interval.

The verdict logic is NOT duplicated here: ``derive_flow_state`` is imported from
the pure handler, so the SQL writer and the in-memory writer cannot drift into
disagreeing about what STALLED means.

The ordering rule is enforced in SQL rather than read-then-write: the
``ON CONFLICT ... WHERE`` clause refuses a write whose ``ingest_sequence`` is
older than what is stored for the same node. A read-compare-write would race
under concurrent consumers and let an older redelivery win.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Coroutine, Hashable, Sequence
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

import yaml

from omnimarket.nodes.node_projection_consumer_flow.handlers.handler_projection_consumer_flow import (
    HandlerProjectionConsumerFlow,
    snapshot_publish_due,
)
from omnimarket.nodes.node_projection_consumer_flow.models import (
    ModelConsumerFlowProjectionRequest,
    ModelConsumerFlowRow,
    ModelNodeFlowWindowWire,
    ModelSnapshotPublishPolicy,
)
from omnimarket.projection.discovery import load_projection_exposures_from_contract
from omnimarket.projection.models import ProjectionTableConfig
from omnimarket.projection.runner import BaseProjectionRunner, MessageMeta

if TYPE_CHECKING:
    from omnimarket.adapters.asyncpg_adapter import AsyncpgAdapter

TABLE_FLOW = "omninode_internal.consumer_flow_windows"
TABLE_PRODUCE = "omninode_internal.topic_produce_windows"

# Every per-delta statement is ONE statement per heartbeat, with the deltas bound
# as parallel arrays and expanded by ``unnest`` (OMN-17427, C28). A heartbeat
# carries one window per (consumer_group, topic) the node consumes -- 477 on the
# lab dev lane on 2026-10-22 -- and the statement-per-delta shape spent about
# 5,000 database round trips on each one, so the writer projected about 6
# heartbeats a minute against about 6 arriving and sat 5.5 hours behind.

_UPSERT_PRODUCE = f"""
    INSERT INTO {TABLE_PRODUCE} (
        topic, window_start, window_end, node_id, ingest_sequence,
        messages_produced, evaluated_at
    )
    SELECT * FROM unnest(
        $1::text[], $2::timestamptz[], $3::timestamptz[], $4::uuid[],
        $5::bigint[], $6::bigint[], $7::timestamptz[]
    )
    ON CONFLICT (topic, window_start) DO UPDATE SET
        window_end = EXCLUDED.window_end,
        node_id = EXCLUDED.node_id,
        ingest_sequence = EXCLUDED.ingest_sequence,
        messages_produced = EXCLUDED.messages_produced,
        evaluated_at = EXCLUDED.evaluated_at
"""

# One row per probed (topic, window_start, window_end), named by its position in
# the bound arrays. ``window_count`` 0 is the "no producer visible" answer the
# derivation needs as None, never as a produced total of 0.
_SELECT_UPSTREAM = f"""
    SELECT probe.ordinal AS ordinal,
           upstream.window_count AS window_count,
           upstream.produced AS produced
    FROM unnest($1::text[], $2::timestamptz[], $3::timestamptz[])
         WITH ORDINALITY AS probe(topic, window_start, window_end, ordinal)
    CROSS JOIN LATERAL (
        SELECT COUNT(*) AS window_count,
               COALESCE(SUM(produced.messages_produced), 0) AS produced
        FROM {TABLE_PRODUCE} AS produced
        WHERE produced.topic = probe.topic
          AND produced.window_start < probe.window_end
          AND produced.window_end > probe.window_start
    ) AS upstream
"""

_FLOW_RETURNING = """
    RETURNING consumer_group, topic, window_start, window_end, node_id,
              ingest_sequence, messages_in, messages_out, messages_dlq,
              handler_errors, upstream_produced, upstream_evidence,
              flow_state, evaluated_at, projection_cursor
"""

# The conflict target is the primary key, and one INSERT may not touch the same
# key twice, so callers bind keys that are unique within one statement (see
# ``_unique_key_chunks``).
_UPSERT_FLOW = f"""
    INSERT INTO {TABLE_FLOW} (
        consumer_group, topic, window_start, window_end, node_id,
        ingest_sequence, messages_in, messages_out, messages_dlq,
        handler_errors, upstream_produced, upstream_evidence, flow_state,
        evaluated_at
    )
    SELECT * FROM unnest(
        $1::text[], $2::text[], $3::timestamptz[], $4::timestamptz[],
        $5::uuid[], $6::bigint[], $7::bigint[], $8::bigint[], $9::bigint[],
        $10::bigint[], $11::bigint[], $12::text[], $13::text[],
        $14::timestamptz[]
    )
    ON CONFLICT (consumer_group, topic, window_start) DO UPDATE SET
        window_end = EXCLUDED.window_end,
        node_id = EXCLUDED.node_id,
        ingest_sequence = EXCLUDED.ingest_sequence,
        messages_in = EXCLUDED.messages_in,
        messages_out = EXCLUDED.messages_out,
        messages_dlq = EXCLUDED.messages_dlq,
        handler_errors = EXCLUDED.handler_errors,
        upstream_produced = EXCLUDED.upstream_produced,
        upstream_evidence = EXCLUDED.upstream_evidence,
        flow_state = EXCLUDED.flow_state,
        evaluated_at = EXCLUDED.evaluated_at
    WHERE {TABLE_FLOW}.node_id <> EXCLUDED.node_id
       OR {TABLE_FLOW}.ingest_sequence <= EXCLUDED.ingest_sequence
{_FLOW_RETURNING}"""

# A gap row is written only into an empty slot: UNKNOWN is strictly less
# informative than any observed window, so a late heartbeat must never be
# downgraded by one. DO NOTHING also absorbs a key repeated within the batch.
_INSERT_UNKNOWN = f"""
    INSERT INTO {TABLE_FLOW} (
        consumer_group, topic, window_start, window_end, node_id,
        ingest_sequence, upstream_evidence, flow_state, evaluated_at
    )
    SELECT * FROM unnest(
        $1::text[], $2::text[], $3::timestamptz[], $4::timestamptz[],
        $5::uuid[], $6::bigint[], $7::text[], $8::text[], $9::timestamptz[]
    )
    ON CONFLICT (consumer_group, topic, window_start) DO NOTHING
{_FLOW_RETURNING}"""

_SELECT_PRIOR_STATE = f"""
    SELECT MAX(ingest_sequence) AS last_sequence
    FROM {TABLE_FLOW}
    WHERE node_id = $1
"""

_SELECT_KEYS_AT_SEQUENCE = f"""
    SELECT DISTINCT consumer_group, topic
    FROM {TABLE_FLOW}
    WHERE node_id = $1 AND ingest_sequence = $2
"""


def _unique_key_chunks[T](
    items: Sequence[T], key: Callable[[T], Hashable]
) -> list[list[T]]:
    """Split ``items`` in order into runs whose keys are unique within a run.

    ``INSERT ... ON CONFLICT DO UPDATE`` refuses to touch one row twice in a
    single statement. A heartbeat names each (consumer_group, topic) once, so
    this is one chunk in practice; a repeated key starts a new chunk, so the
    later delta still lands after the earlier one, as the row-at-a-time writer
    applied them.
    """
    chunks: list[list[T]] = []
    seen: set[Hashable] = set()
    for item in items:
        item_key = key(item)
        if not chunks or item_key in seen:
            chunks.append([])
            seen = set()
        chunks[-1].append(item)
        seen.add(item_key)
    return chunks


def _row_key(row: ModelConsumerFlowRow) -> tuple[str, str, datetime]:
    return (row.consumer_group, row.topic, row.window_start)


def _in_input_order(
    returned: list[dict[str, Any]], rows: Sequence[ModelConsumerFlowRow]
) -> list[dict[str, Any]]:
    """Order ``RETURNING`` rows as their inputs were bound.

    Postgres does not promise ``RETURNING`` order for a multi-row insert, and
    the applied payload and the snapshot publishes follow it.
    """
    position = {_row_key(row): index for index, row in enumerate(rows)}

    def _position(record: dict[str, Any]) -> int:
        key = (
            str(record["consumer_group"]),
            str(record["topic"]),
            record["window_start"],
        )
        return position.get(key, len(position))

    return sorted(returned, key=_position)


def _wire_row(row: dict[str, Any]) -> dict[str, Any]:
    """Render one accepted database row in a form the event bus can carry.

    The row travels onward as the applied event's payload, so it is reduced to
    JSON primitives here rather than at the transport, where a value the
    encoder cannot handle would take the whole terminal event — and with it the
    downstream alert's only trigger — down with it.

    Counters are NOT coerced: an ``UNKNOWN`` window carries ``None``, never
    ``0``. Collapsing that to zero would let an unobserved window read as
    observed-and-idle, which is the false-green this projection exists to close.
    """
    wired: dict[str, Any] = {}
    for key, value in row.items():
        if isinstance(value, datetime):
            wired[key] = value.isoformat()
        elif isinstance(value, UUID):
            wired[key] = str(value)
        elif isinstance(value, Enum):
            wired[key] = value.value
        else:
            wired[key] = value
    return wired


class ConsumerFlowProjectionWriter(BaseProjectionRunner):
    """Projects heartbeat flow windows into ``consumer_flow_windows``.

    Named ``...Writer`` rather than ``...Runner`` or ``Handler...`` on purpose,
    and both halves of that matter: ``Runner`` is a non-canonical type-word the
    OMN-14350 ratchet hard-fails (and its allowlist may only shrink, so a new
    entry is not an option), while a ``Handler``-prefixed class is required by
    the OMN-10821 wiring check to be imported from a Python module — which
    would drag the whole aiokafka projection-runner stack into this node's
    import path for every consumer of the pure handler. ``Writer`` is also just
    the accurate word: this is the projection writer, the same role the
    deployed ``*-writer`` services already carry.

    That naming choice used to decide, silently, which of the runtime's two
    projection dispatch branches this class landed on: the auto-wiring tested
    ``type(handler).__name__.endswith("ProjectionRunner")``. Satisfying the
    type-word ratchet therefore moved this node onto the DB-injection branch,
    where the runtime pre-connected this class's asyncpg pool on a throwaway
    event loop and every message afterwards died with ``Event loop is closed``.
    Dispatch shape is now declared below instead of inferred from a spelling,
    and the declaration carries an obligation this class has to keep.
    """

    #: This class is dispatched IN-PROCESS by the runtime auto-wiring, once per
    #: consumed message, rather than driven by its own ``run()`` consume loop
    #: the way the ``*ProjectionRunner`` siblings are. The runtime reads this
    #: attribute by name; it is a plain class attribute rather than an import so
    #: this module keeps no import-time dependency on the runtime package.
    #:
    #: Declaring it is a promise. Every loop-bound resource this class touches
    #: — the asyncpg pool and the snapshot producer both — lives on the
    #: writer's own dispatch loop, one long-lived event loop on a daemon thread
    #: that ``handle()`` submits each message to. The runtime cannot scope the
    #: lifetime of a pool it did not create, and no longer tries to.
    onex_runtime_inprocess_dispatch = True

    def __init__(self, contract_path: Path | None = None) -> None:
        super().__init__()
        _path = contract_path or Path(__file__).parent.parent / "contract.yaml"
        with open(_path) as handle:
            self._contract: dict[str, Any] = yaml.safe_load(handle)
        self._snapshot_publish_policy = ModelSnapshotPublishPolicy.model_validate(
            self._contract["snapshot_publish_policy"]
        )

        node_name = str(self._contract.get("name", "projection_consumer_flow"))
        exposures = load_projection_exposures_from_contract(
            self._contract, node_name, _path
        )
        self._snapshot_exposure: ProjectionTableConfig | None = next(
            (exposure for exposure in exposures if exposure.bus_backed), None
        )
        if self._snapshot_exposure is not None:
            for column in self._snapshot_publish_policy.verdict_columns:
                if column not in self._snapshot_exposure.columns:
                    raise ValueError(
                        f"Snapshot verdict column {column!r} is not in exposure columns"
                    )
        self._last_published: dict[
            tuple[str, str], tuple[tuple[str, ...], datetime]
        ] = {}
        # The in-process dispatch loop (OMN-17427, C28). Started on the first
        # message and kept for the writer's life, so the pool and the producer
        # opened on it are opened once, not once per heartbeat.
        self._dispatch_guard = threading.Lock()
        self._dispatch_loop: asyncio.AbstractEventLoop | None = None
        self._dispatch_thread: threading.Thread | None = None
        self._connect_lock: asyncio.Lock | None = None
        # The adapter whose pool this writer opened, compared by identity so a
        # replaced ``_db`` (the runtime's bind, a test double) is connected.
        self._connected_db: AsyncpgAdapter | None = None
        # Cold start is self-healing and deliberately has no backfill publisher:
        # every live runtime re-emits a heartbeat on its own interval. The first
        # window per pair after start publishes, then on verdict change or once
        # per refresh interval. The bus-backed cache
        # therefore reaches steady state within one heartbeat interval of
        # startup. A runtime that is NOT heartbeating publishes nothing — which
        # is the correct answer, because a runtime that stopped heartbeating is
        # exactly what this projection is supposed to stop vouching for.

    @property
    def subscribe_topics(self) -> list[str]:
        return list(self._contract.get("event_bus", {}).get("subscribe_topics", []))

    @property
    def topics(self) -> list[str]:
        return self.subscribe_topics

    def handle(self, input_data: dict[str, Any]) -> dict[str, Any]:
        """RuntimeLocal handler protocol shim: one loop and one pool per writer.

        The runtime calls this from a worker thread (``asyncio.to_thread``), so
        the message is handed to the writer's own dispatch loop and this thread
        waits for its answer. It used to call ``asyncio.run`` here, which opened
        a new loop, a new asyncpg pool and a new Kafka producer for every
        heartbeat and closed all three on return (OMN-17427, C28: the lab dev
        lane fell 5.5 hours behind at about 6 heartbeats a minute).
        """
        topics = self.subscribe_topics
        topic = str(input_data.pop("_topic", topics[0] if topics else ""))
        meta = MessageMeta(
            partition=int(input_data.pop("_partition", 0)),
            offset=int(input_data.pop("_offset", 0)),
            fallback_id=str(input_data.pop("_fallback_id", "")),
            topic=topic,
        )
        return self._run_on_dispatch_loop(
            self._project_one_message(topic, input_data, meta)
        )

    def _run_on_dispatch_loop[T](self, coroutine: Coroutine[Any, Any, T]) -> T:
        """Run ``coroutine`` on the long-lived dispatch loop and wait for it."""
        with self._dispatch_guard:
            loop = self._dispatch_loop
            if loop is None or loop.is_closed():
                loop = asyncio.new_event_loop()
                thread = threading.Thread(
                    target=loop.run_forever,
                    name=f"{type(self).__name__}-dispatch",
                    daemon=True,
                )
                thread.start()
                self._dispatch_loop = loop
                self._dispatch_thread = thread
                self._connect_lock = None
                self._connected_db = None
        return asyncio.run_coroutine_threadsafe(coroutine, loop).result()

    def close_dispatch(self) -> None:
        """Close the pool and producer and stop the dispatch loop, if started."""
        with self._dispatch_guard:
            loop = self._dispatch_loop
            thread = self._dispatch_thread
            self._dispatch_loop = None
            self._dispatch_thread = None
        if loop is None or loop.is_closed():
            return
        asyncio.run_coroutine_threadsafe(self.aclose(), loop).result()
        loop.call_soon_threadsafe(loop.stop)
        if thread is not None:
            thread.join()
        loop.close()

    async def aclose(self) -> None:
        """Release what the message path opened on the current loop."""
        await self._stop_producer()
        if self._connected_db is not None:
            connected = self._connected_db
            self._connected_db = None
            await connected.close()

    async def _ensure_connected(self) -> None:
        """Open the pool once on the running loop, and again only if ``_db`` changed."""
        if self._connected_db is self.db:
            return
        if self._connect_lock is None:
            self._connect_lock = asyncio.Lock()
        async with self._connect_lock:
            if self._connected_db is self.db:
                return
            await self.db.connect()
            self._connected_db = self.db

    async def _project_one_message(
        self, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> dict[str, Any]:
        """Project one runtime-dispatched message and report what was written.

        Runs on the writer's dispatch loop, where the pool and the snapshot
        producer stay open between messages. An asyncpg pool (or an
        ``AIOKafkaProducer``) is bound to the loop that created it, which is why
        they once had to be opened and closed per message under
        ``asyncio.run`` (``RuntimeError: Event loop is closed``, OMN-16874);
        one loop that outlives the messages keeps that guarantee without paying
        a connect per heartbeat.

        The standalone ``run()`` path does NOT come through here: it owns one
        long-lived loop, connects once, and reuses both resources for the life
        of the process.

        The returned mapping is the applied event's payload — ``handler_wiring``
        publishes it to ``projection-consumer-flow-applied.v1`` — so it carries
        the window facts a downstream consumer needs, not a bare ack.
        """
        await self._ensure_connected()
        written, refused = await self._project_window_with_refusals(topic, data, meta)
        return {
            "rows_upserted": len(written),
            "flow_rows": written,
            "rows_refused_by_ordering_guard": refused,
        }

    async def project_event(
        self, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> bool:
        """Standalone-runner entrypoint: project one message, report success.

        Kept boolean because :class:`BaseProjectionRunner`'s consume loop is its
        caller and commits offsets on that answer. The in-process dispatch path
        uses :meth:`_project_one_message`, which reports the rows themselves.
        """
        await self._project_window(topic, data, meta)
        return True

    async def _project_window(
        self, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> list[dict[str, Any]]:
        """Project a standalone window, retaining the accepted-row interface."""
        accepted, _ = await self._project_window_with_refusals(topic, data, meta)
        return accepted

    async def _project_window_with_refusals(
        self, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> tuple[list[dict[str, Any]], int]:
        """Resolve the facts the derivation cannot see, then persist its output.

        The verdict logic is NOT duplicated here: this reads the database,
        hands the facts to the pure def-B handler, and writes what it returns.
        A second copy of the derivation is how a SQL writer and an in-memory
        writer drift into disagreeing about what STALLED means.

        Returns accepted rows in wire-safe form and the number of observed
        upserts refused by the SQL ordering predicate. Only an empty RETURNING
        from that upsert counts: missing windows and gap inserts that find an
        occupied slot are not ordering refusals. Counts belong to this call,
        never to writer state shared across messages.
        """
        raw_window = data.get("flow_window")
        if raw_window is None:
            # No window on this heartbeat: the priming tick, or another node in
            # the process carries the window. Absence is not zero traffic, so
            # nothing is written.
            return [], 0
        window = ModelNodeFlowWindowWire.model_validate(raw_window)

        for chunk in _unique_key_chunks(
            window.produce_deltas, lambda produce: (produce.topic, produce.window_start)
        ):
            await self.db.execute(
                _UPSERT_PRODUCE,
                [produce.topic for produce in chunk],
                [produce.window_start for produce in chunk],
                [produce.window_end for produce in chunk],
                [str(produce.node_id) for produce in chunk],
                [produce.window_sequence for produce in chunk],
                [produce.messages_produced for produce in chunk],
                [produce.window_end for produce in chunk],
            )

        upstream = await self._upstream_produced_by_topic(
            [
                (delta.topic, delta.window_start, delta.window_end)
                for delta in window.consumer_deltas
            ]
        )

        last_sequence, known_keys = await self._prior_state(str(window.node_id))
        result = HandlerProjectionConsumerFlow().handle(
            ModelConsumerFlowProjectionRequest(
                flow_window=window,
                upstream_produced_by_topic=upstream,
                last_observed_sequence=last_sequence,
                known_keys=known_keys,
            )
        )

        accepted: list[dict[str, Any]] = []
        gap_written: list[dict[str, Any]] = []
        unknown_rows = result.unknown_rows
        if unknown_rows:
            gap_rows = await self.db.execute(
                _INSERT_UNKNOWN,
                [unknown.consumer_group for unknown in unknown_rows],
                [unknown.topic for unknown in unknown_rows],
                [unknown.window_start for unknown in unknown_rows],
                [unknown.window_end for unknown in unknown_rows],
                [str(unknown.node_id) for unknown in unknown_rows],
                [unknown.ingest_sequence for unknown in unknown_rows],
                [unknown.upstream_evidence.value for unknown in unknown_rows],
                [unknown.flow_state.value for unknown in unknown_rows],
                [unknown.evaluated_at for unknown in unknown_rows],
            )
            gap_written.extend(_in_input_order(gap_rows or [], unknown_rows))

        observed: list[dict[str, Any]] = []
        refused = 0
        for flow_chunk in _unique_key_chunks(result.flow_rows, _row_key):
            written = await self._upsert_flow_rows(flow_chunk)
            observed.extend(written)
            refused += len(flow_chunk) - len(written)

        # AC6. The snapshot is keyed on (consumer_group, topic) alone, and both
        # rows of a pair carry THIS message's coordinates, so publishing a gap
        # delta beside the pair's observed one makes the two collide on the
        # same cache key at the same source offset -- and
        # SnapshotCache.apply_message drops the second as a stale replay,
        # leaving the exposure serving UNKNOWN with null counters for a pair
        # whose window this very invocation observed. The gap row also does not
        # survive in the TABLE: it is minted at the arriving window's own
        # window_start, so the flow upsert that follows overwrites it on the
        # same primary key. A gap delta for such a pair therefore states
        # nothing the database holds, and is not published. Suppression is
        # keyed on the rows the database ACCEPTED, so a pair whose observed
        # write was refused by the ordering predicate still gets its gap delta.
        observed_pairs = {(row["consumer_group"], row["topic"]) for row in observed}
        for gap_row in gap_written:
            if (gap_row["consumer_group"], gap_row["topic"]) not in observed_pairs:
                await self._publish_snapshot_if_available(gap_row, meta, data)
            accepted.append(_wire_row(gap_row))

        for written_row in observed:
            await self._publish_snapshot_if_available(written_row, meta, data)
            accepted.append(_wire_row(written_row))
        return accepted, refused

    async def _upsert_flow_rows(
        self, rows: Sequence[ModelConsumerFlowRow]
    ) -> list[dict[str, Any]]:
        """Upsert rows with unique keys in one statement; return those accepted.

        A row the ordering predicate refused is absent from ``RETURNING``, so
        ``len(rows) - len(returned)`` is the refusal count.
        """
        returned = await self.db.execute(
            _UPSERT_FLOW,
            [row.consumer_group for row in rows],
            [row.topic for row in rows],
            [row.window_start for row in rows],
            [row.window_end for row in rows],
            [str(row.node_id) for row in rows],
            [row.ingest_sequence for row in rows],
            [row.messages_in for row in rows],
            [row.messages_out for row in rows],
            [row.messages_dlq for row in rows],
            [row.handler_errors for row in rows],
            [row.upstream_produced for row in rows],
            [row.upstream_evidence.value for row in rows],
            [row.flow_state.value for row in rows],
            [row.evaluated_at for row in rows],
        )
        return _in_input_order(returned or [], rows)

    async def _prior_state(
        self, node_id: str
    ) -> tuple[int | None, tuple[tuple[str, str], ...]]:
        """The highest window this node already delivered, and its keys.

        Both are handed to the pure derivation rather than looked up inside it:
        a gap is only detectable against what was already materialized, and
        that is a database fact, not a property of the event.
        """
        rows = await self.db.execute(_SELECT_PRIOR_STATE, node_id)
        if not rows:
            return None, ()
        last_raw = rows[0].get("last_sequence")
        if last_raw is None:
            return None, ()
        key_rows = await self.db.execute(
            _SELECT_KEYS_AT_SEQUENCE, node_id, int(last_raw)
        )
        keys = tuple(
            (str(r["consumer_group"]), str(r["topic"])) for r in (key_rows or [])
        )
        return int(last_raw), keys

    async def _upstream_produced_by_topic(
        self, probes: Sequence[tuple[str, datetime, datetime]]
    ) -> dict[str, int]:
        """Production to each probed topic over overlapping windows, in one read.

        A topic with no producing window ever recorded is left out, never set
        to 0: nothing in this runtime publishes there and an external producer
        is invisible on this rail. Collapsing that into 0 would let the
        derivation call an externally-fed topic STARVED on no evidence. When a
        topic is probed more than once, the last probe that saw a producer
        stands, as it did when each delta was read on its own.
        """
        distinct = list(dict.fromkeys(probes))
        if not distinct:
            return {}
        rows = await self.db.execute(
            _SELECT_UPSTREAM,
            [topic for topic, _, _ in distinct],
            [window_start for _, window_start, _ in distinct],
            [window_end for _, _, window_end in distinct],
        )
        answers: dict[tuple[str, datetime, datetime], int | None] = {}
        for row in rows or []:
            probe = distinct[int(row["ordinal"]) - 1]
            count = int(row.get("window_count") or 0)
            answers[probe] = int(row.get("produced") or 0) if count else None
        upstream: dict[str, int] = {}
        for probe in probes:
            produced = answers.get(probe)
            if produced is not None:
                upstream[probe[0]] = produced
        return upstream

    async def _publish_snapshot_if_available(
        self, row: dict[str, Any] | None, meta: MessageMeta, data: dict[str, Any]
    ) -> None:
        """Best-effort snapshot publish: a no-op unless this node declares a
        bus_backed exposure AND the write returned a real row."""
        if self._snapshot_exposure is None or row is None:
            return
        policy = self._snapshot_publish_policy
        key = (str(row["consumer_group"]), str(row["topic"]))
        verdict = tuple(str(row[column]) for column in policy.verdict_columns)
        window_end = row["window_end"]
        if not isinstance(window_end, datetime):
            window_end = datetime.fromisoformat(window_end)
        if not snapshot_publish_due(
            policy=policy,
            last_published=self._last_published.get(key),
            verdict=verdict,
            window_end=window_end,
        ):
            return
        source_event_id = str(data.get("correlation_id") or meta.fallback_id)
        await self.publish_snapshot_delta(
            self._snapshot_exposure,
            op="upsert",
            row=row,
            source_event_id=source_event_id,
            source_topic=meta.topic,
            source_partition=meta.partition,
            source_offset=meta.offset,
        )
        self._last_published[key] = (verdict, window_end)


__all__ = ["ConsumerFlowProjectionWriter"]
