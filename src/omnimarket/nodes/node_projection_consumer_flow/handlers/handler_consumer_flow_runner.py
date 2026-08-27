# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Standalone projection runner for consumer flow windows (OMN-16777).

Consumes ``onex.evt.platform.node-heartbeat.v1`` — the topic the runtime already
emits on — and writes the flow rows the read model serves, then publishes each
written row as a snapshot delta so the exposure is genuinely bus-backed.

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
from pathlib import Path
from typing import Any

import yaml

from omnimarket.nodes.node_projection_consumer_flow.handlers.handler_projection_consumer_flow import (
    HandlerProjectionConsumerFlow,
)
from omnimarket.nodes.node_projection_consumer_flow.models import (
    ModelConsumerFlowProjectionRequest,
    ModelConsumerFlowRow,
    ModelNodeFlowWindowWire,
)
from omnimarket.projection.discovery import load_projection_exposures_from_contract
from omnimarket.projection.models import ProjectionTableConfig
from omnimarket.projection.runner import BaseProjectionRunner, MessageMeta

TABLE_FLOW = "omninode_internal.consumer_flow_windows"
TABLE_PRODUCE = "omninode_internal.topic_produce_windows"

_UPSERT_PRODUCE = f"""
    INSERT INTO {TABLE_PRODUCE} (
        topic, window_start, window_end, node_id, ingest_sequence,
        messages_produced, evaluated_at
    )
    VALUES ($1, $2, $3, $4, $5, $6, $7)
    ON CONFLICT (topic, window_start) DO UPDATE SET
        window_end = EXCLUDED.window_end,
        node_id = EXCLUDED.node_id,
        ingest_sequence = EXCLUDED.ingest_sequence,
        messages_produced = EXCLUDED.messages_produced,
        evaluated_at = EXCLUDED.evaluated_at
"""

_SELECT_UPSTREAM = f"""
    SELECT COUNT(*) AS window_count,
           COALESCE(SUM(messages_produced), 0) AS produced
    FROM {TABLE_PRODUCE}
    WHERE topic = $1 AND window_start < $3 AND window_end > $2
"""

_UPSERT_FLOW = f"""
    INSERT INTO {TABLE_FLOW} (
        consumer_group, topic, window_start, window_end, node_id,
        ingest_sequence, messages_in, messages_out, messages_dlq,
        handler_errors, upstream_produced, upstream_evidence, flow_state,
        evaluated_at
    )
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)
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
    RETURNING consumer_group, topic, window_start, window_end, node_id,
              ingest_sequence, messages_in, messages_out, messages_dlq,
              handler_errors, upstream_produced, upstream_evidence,
              flow_state, evaluated_at
"""

# A gap row is written only into an empty slot: UNKNOWN is strictly less
# informative than any observed window, so a late heartbeat must never be
# downgraded by one.
_INSERT_UNKNOWN = f"""
    INSERT INTO {TABLE_FLOW} (
        consumer_group, topic, window_start, window_end, node_id,
        ingest_sequence, upstream_evidence, flow_state, evaluated_at
    )
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
    ON CONFLICT (consumer_group, topic, window_start) DO NOTHING
    RETURNING consumer_group, topic, window_start, window_end, node_id,
              ingest_sequence, messages_in, messages_out, messages_dlq,
              handler_errors, upstream_produced, upstream_evidence,
              flow_state, evaluated_at
"""

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
    """

    def __init__(self, contract_path: Path | None = None) -> None:
        super().__init__()
        _path = contract_path or Path(__file__).parent.parent / "contract.yaml"
        with open(_path) as handle:
            self._contract: dict[str, Any] = yaml.safe_load(handle)

        node_name = str(self._contract.get("name", "projection_consumer_flow"))
        exposures = load_projection_exposures_from_contract(
            self._contract, node_name, _path
        )
        self._snapshot_exposure: ProjectionTableConfig | None = next(
            (exposure for exposure in exposures if exposure.bus_backed), None
        )
        # Cold start is self-healing and deliberately has no backfill publisher:
        # every live runtime re-emits a heartbeat on its own interval, and every
        # heartbeat carrying a window republishes its rows. The bus-backed cache
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
        """RuntimeLocal handler protocol shim."""
        topics = self.subscribe_topics
        topic = str(input_data.pop("_topic", topics[0] if topics else ""))
        meta = MessageMeta(
            partition=int(input_data.pop("_partition", 0)),
            offset=int(input_data.pop("_offset", 0)),
            fallback_id=str(input_data.pop("_fallback_id", "")),
            topic=topic,
        )
        projected = asyncio.run(self.project_event(topic, input_data, meta))
        return {"projected": projected}

    async def project_event(
        self, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> bool:
        """Resolve the facts the derivation cannot see, then persist its output.

        The verdict logic is NOT duplicated here: this reads the database,
        hands the facts to the pure def-B handler, and writes what it returns.
        A second copy of the derivation is how a SQL writer and an in-memory
        writer drift into disagreeing about what STALLED means.
        """
        raw_window = data.get("flow_window")
        if raw_window is None:
            # No window on this heartbeat: the priming tick, or another node in
            # the process carries the window. Absence is not zero traffic, so
            # nothing is written.
            return True
        window = ModelNodeFlowWindowWire.model_validate(raw_window)

        for produce in window.produce_deltas:
            await self.db.execute(
                _UPSERT_PRODUCE,
                produce.topic,
                produce.window_start,
                produce.window_end,
                str(produce.node_id),
                produce.window_sequence,
                produce.messages_produced,
                produce.window_end,
            )

        upstream: dict[str, int] = {}
        for delta in window.consumer_deltas:
            produced = await self._upstream_produced(
                delta.topic, delta.window_start, delta.window_end
            )
            if produced is not None:
                upstream[delta.topic] = produced

        last_sequence, known_keys = await self._prior_state(str(window.node_id))
        result = HandlerProjectionConsumerFlow().handle(
            ModelConsumerFlowProjectionRequest(
                flow_window=window,
                upstream_produced_by_topic=upstream,
                last_observed_sequence=last_sequence,
                known_keys=known_keys,
            )
        )

        for unknown in result.unknown_rows:
            gap_rows = await self.db.execute(
                _INSERT_UNKNOWN,
                unknown.consumer_group,
                unknown.topic,
                unknown.window_start,
                unknown.window_end,
                str(unknown.node_id),
                unknown.ingest_sequence,
                unknown.upstream_evidence.value,
                unknown.flow_state.value,
                unknown.evaluated_at,
            )
            for gap_row in gap_rows or []:
                await self._publish_snapshot_if_available(gap_row, meta, data)

        for row in result.flow_rows:
            written = await self._upsert_flow_row(row)
            await self._publish_snapshot_if_available(written, meta, data)
        return True

    async def _upsert_flow_row(
        self, row: ModelConsumerFlowRow
    ) -> dict[str, Any] | None:
        rows = await self.db.execute(
            _UPSERT_FLOW,
            row.consumer_group,
            row.topic,
            row.window_start,
            row.window_end,
            str(row.node_id),
            row.ingest_sequence,
            row.messages_in,
            row.messages_out,
            row.messages_dlq,
            row.handler_errors,
            row.upstream_produced,
            row.upstream_evidence.value,
            row.flow_state.value,
            row.evaluated_at,
        )
        return rows[0] if rows else None

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

    async def _upstream_produced(
        self, topic: str, window_start: Any, window_end: Any
    ) -> int | None:
        """Production to ``topic`` over overlapping windows, or None if unseen.

        None is not 0: it means no producing window for this topic has ever been
        recorded, so nothing in this runtime publishes there and an external
        producer is invisible on this rail. Collapsing that into 0 would let the
        derivation call an externally-fed topic STARVED on no evidence.
        """
        rows = await self.db.execute(_SELECT_UPSTREAM, topic, window_start, window_end)
        if not rows:
            return None
        row = rows[0]
        if not int(row.get("window_count") or 0):
            return None
        return int(row.get("produced") or 0)

    async def _publish_snapshot_if_available(
        self, row: dict[str, Any] | None, meta: MessageMeta, data: dict[str, Any]
    ) -> None:
        """Best-effort snapshot publish: a no-op unless this node declares a
        bus_backed exposure AND the write returned a real row."""
        if self._snapshot_exposure is None or row is None:
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


__all__ = ["ConsumerFlowProjectionWriter"]
