# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Projection writer for ranked runtime-error fingerprints (OMN-18770).

Consumes ``onex.evt.omnibase-infra.runtime-error.v1`` — the topic the runtime
log bridge already publishes on — writes the ranked rows the read model serves,
and publishes each written row as a snapshot delta so the exposure is genuinely
bus-backed rather than declared so.

The classification logic is NOT duplicated here: ``derive_error_category`` and
``derive_fingerprint`` are imported from the pure handler, so the SQL writer and
the in-memory derivation cannot drift into disagreeing about what a
``database`` error is or which fingerprint an error class has.

The accumulation rule is enforced in SQL rather than read-then-write for the
count, and read-then-derive only for the facts the pure handler needs: an
``ON CONFLICT ... DO UPDATE`` adds the arriving occurrences to the stored total
atomically, so two concurrent consumers cannot lose a count between a read and
a write. ``last_seen_at``/``correlation_id`` advance only when the arriving
event is NEWER, so a redelivered older occurrence cannot hand the trace widget
a correlation id that has already aged out.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import UUID

import yaml

from omnimarket.nodes.node_projection_runtime_error_fingerprints.handlers.handler_projection_runtime_error_fingerprints import (
    HandlerProjectionRuntimeErrorFingerprints,
)
from omnimarket.nodes.node_projection_runtime_error_fingerprints.models import (
    ModelRuntimeErrorEventWire,
    ModelRuntimeErrorFingerprintRequest,
)
from omnimarket.projection.discovery import load_projection_exposures_from_contract
from omnimarket.projection.models import ProjectionTableConfig
from omnimarket.projection.runner import BaseProjectionRunner, MessageMeta

logger = logging.getLogger(__name__)

TABLE = "omninode_internal.runtime_error_fingerprints"

# The prior state the pure derivation needs, and nothing else. Read before the
# write because the accumulated count and the earliest sighting are database
# facts, not properties of the event — the same split the consumer-flow writer
# makes for exactly the same reason.
_SELECT_PRIOR = f"""
    SELECT occurrence_count, first_seen_at, last_seen_at
    FROM {TABLE}
    WHERE fingerprint = $1
"""

# occurrence_count is added IN SQL, not written from the value the pure
# handler computed: the handler's total is derived from a count read a moment
# earlier, and between that read and this write a concurrent consumer may have
# added its own. `EXCLUDED.occurrence_count` carries only THIS event's
# occurrences, so the addition is atomic and no count is lost.
#
# Everything describing the LATEST occurrence — correlation_id, severity,
# hostname, service, exception — advances only when the arriving event is at
# least as new as what is stored. A redelivered older occurrence still counts
# (it happened) but must not overwrite a live correlation id with a dead one.
_UPSERT = f"""
    INSERT INTO {TABLE} (
        fingerprint, logger_name, error_category, category_evidence, severity,
        message_template, exception_type, occurrence_count, correlation_id,
        service_name, hostname, first_seen_at, last_seen_at
    )
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
    ON CONFLICT (fingerprint) DO UPDATE SET
        occurrence_count = {TABLE}.occurrence_count + EXCLUDED.occurrence_count,
        first_seen_at = LEAST({TABLE}.first_seen_at, EXCLUDED.first_seen_at),
        last_seen_at = GREATEST({TABLE}.last_seen_at, EXCLUDED.last_seen_at),
        error_category = CASE WHEN EXCLUDED.last_seen_at >= {TABLE}.last_seen_at
                              THEN EXCLUDED.error_category
                              ELSE {TABLE}.error_category END,
        category_evidence = CASE WHEN EXCLUDED.last_seen_at >= {TABLE}.last_seen_at
                                 THEN EXCLUDED.category_evidence
                                 ELSE {TABLE}.category_evidence END,
        severity = CASE WHEN EXCLUDED.last_seen_at >= {TABLE}.last_seen_at
                        THEN EXCLUDED.severity ELSE {TABLE}.severity END,
        exception_type = CASE WHEN EXCLUDED.last_seen_at >= {TABLE}.last_seen_at
                              THEN EXCLUDED.exception_type
                              ELSE {TABLE}.exception_type END,
        correlation_id = CASE WHEN EXCLUDED.last_seen_at >= {TABLE}.last_seen_at
                              THEN EXCLUDED.correlation_id
                              ELSE {TABLE}.correlation_id END,
        service_name = CASE WHEN EXCLUDED.last_seen_at >= {TABLE}.last_seen_at
                            THEN EXCLUDED.service_name
                            ELSE {TABLE}.service_name END,
        hostname = CASE WHEN EXCLUDED.last_seen_at >= {TABLE}.last_seen_at
                        THEN EXCLUDED.hostname ELSE {TABLE}.hostname END
    RETURNING fingerprint, logger_name, error_category, category_evidence,
              severity, message_template, exception_type, occurrence_count,
              correlation_id, service_name, hostname, first_seen_at,
              last_seen_at, projection_cursor
"""


def _wire_row(row: dict[str, Any]) -> dict[str, Any]:
    """Render one accepted database row in a form the event bus can carry.

    The row travels onward as the applied event's payload, so it is reduced to
    JSON primitives here rather than at the transport, where a value the
    encoder cannot handle would take the terminal event down with it.
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


class RuntimeErrorFingerprintProjectionWriter(BaseProjectionRunner):
    """Projects runtime-error events into ranked fingerprint rows.

    Named ``...Writer`` rather than ``...Runner``: ``Runner`` is a
    non-canonical type-word the OMN-14350 ratchet hard-fails and whose
    allowlist may only shrink, while a ``Handler``-prefixed class is required
    by the OMN-10821 wiring check to be imported from a Python module — which
    would drag the aiokafka projection-runner stack into the import path of
    every consumer of the pure handler. ``Writer`` is also the accurate word.
    """

    #: Dispatched IN-PROCESS by the runtime auto-wiring, once per consumed
    #: message, rather than driven by its own ``run()`` consume loop. The
    #: runtime reads this attribute BY NAME; it is a plain class attribute
    #: rather than an import so this module keeps no import-time dependency on
    #: the runtime package.
    #:
    #: Declaring it is a promise this class has to keep: ``handle()`` opens one
    #: event loop per message, so every loop-bound resource it touches — the
    #: asyncpg pool and the snapshot producer both — is opened and closed
    #: inside that loop. Caching either across calls binds it to a loop that no
    #: longer exists and every message afterwards dies with "Event loop is
    #: closed" while offsets keep advancing (OMN-16874, 34 of them on the dev
    #: lane, zero rows written).
    onex_runtime_inprocess_dispatch = True

    def __init__(self, contract_path: Path | None = None) -> None:
        super().__init__()
        _path = contract_path or Path(__file__).parent.parent / "contract.yaml"
        with open(_path) as handle:
            self._contract: dict[str, Any] = yaml.safe_load(handle)

        node_name = str(
            self._contract.get("name", "projection_runtime_error_fingerprints")
        )
        exposures = load_projection_exposures_from_contract(
            self._contract, node_name, _path
        )
        self._snapshot_exposure: ProjectionTableConfig | None = next(
            (exposure for exposure in exposures if exposure.bus_backed), None
        )
        self._derive = HandlerProjectionRuntimeErrorFingerprints()
        # Cold start deliberately has no backfill publisher. Unlike a heartbeat
        # projection, errors are not re-emitted on a timer, so the bus-backed
        # cache after a restart holds only fingerprints seen since. That is the
        # honest answer and it is bounded: the TABLE keeps the full ranked
        # history, and OMN-17345 (the snapshot topics are not actually
        # compacted, so a restart is a long blind replay) is the ticket that
        # owns how fresh the cache is after one.

    @property
    def subscribe_topics(self) -> list[str]:
        return list(self._contract.get("event_bus", {}).get("subscribe_topics", []))

    @property
    def topics(self) -> list[str]:
        return self.subscribe_topics

    @property
    def poison_dlq_topics(self) -> list[str]:
        """OMN-13634: the contract-declared DLQ, READ FROM THE CONTRACT.

        Read rather than restated so the declaration and the behaviour cannot
        diverge. The base class defaults this to an empty list, which means a
        node can declare ``dlq_topics`` in its contract and still quarantine
        nothing — the declaration reads as wired and is not. A projection
        whose entire job is making failure visible must not carry that gap.
        """
        return list(self._contract.get("event_bus", {}).get("dlq_topics", []))

    async def publish_dlq(self, topic: str, value: bytes) -> None:
        """OMN-13634: supply the runtime-owned publisher to the base DLQ path."""
        publish = await self.get_publish_fn()
        if publish is None:
            logger.error(
                "node_projection_runtime_error_fingerprints: no publisher for "
                "POISON DLQ topic %s (event NOT quarantined)",
                topic,
            )
            return
        await publish(topic, value)

    def handle(self, input_data: dict[str, Any]) -> dict[str, Any]:
        """RuntimeLocal handler protocol shim: one message, one loop, one pool."""
        topics = self.subscribe_topics
        topic = str(input_data.pop("_topic", topics[0] if topics else ""))
        meta = MessageMeta(
            partition=int(input_data.pop("_partition", 0)),
            offset=int(input_data.pop("_offset", 0)),
            fallback_id=str(input_data.pop("_fallback_id", "")),
            topic=topic,
        )
        return asyncio.run(self._project_one_message(topic, input_data, meta))

    async def _project_one_message(
        self, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> dict[str, Any]:
        """Project one runtime-dispatched message and report what was written.

        Everything loop-bound is opened and closed inside the single loop
        ``handle()`` opened for this message — see the dispatch declaration
        above for why that is correctness and not tidiness.
        """
        await self.db.connect()
        try:
            written = await self._project_error(topic, data, meta)
        finally:
            await self._stop_producer()
            await self.db.close()
        return {
            "rows_upserted": 0 if written is None else 1,
            "fingerprint_rows": [] if written is None else [written],
        }

    async def project_event(
        self, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> bool:
        """Standalone-runner entrypoint: project one message, report success.

        Boolean because :class:`BaseProjectionRunner`'s consume loop is its
        caller and commits offsets on that answer.
        """
        await self._project_error(topic, data, meta)
        return True

    async def _project_error(
        self, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> dict[str, Any] | None:
        """Derive, persist, and publish one fingerprint row.

        Returns the row the database accepted, in wire-safe form, or ``None``
        when nothing was written. ``None`` is a real answer reported as such,
        never a truthy ack the runtime would read as one row.
        """
        event = ModelRuntimeErrorEventWire.model_validate(data)

        # The fingerprint the write keys on is the DERIVED one, so the prior
        # state has to be looked up under it — not under the producer's
        # fingerprint, which encodes the producer's own (wrong) category.
        probe = self._derive.handle(ModelRuntimeErrorFingerprintRequest(event=event))
        prior_rows = await self.db.execute(_SELECT_PRIOR, probe.row.fingerprint)
        prior = prior_rows[0] if prior_rows else {}

        result = self._derive.handle(
            ModelRuntimeErrorFingerprintRequest(
                event=event,
                prior_occurrence_count=0,  # the count is accumulated in SQL
                prior_first_seen_at=prior.get("first_seen_at"),
            )
        )
        row = result.row

        written = await self.db.execute(
            _UPSERT,
            row.fingerprint,
            row.logger_name,
            row.error_category.value,
            row.category_evidence,
            row.severity.value,
            row.message_template,
            row.exception_type,
            row.occurrence_count,
            row.correlation_id,
            row.service_name,
            row.hostname,
            row.first_seen_at,
            row.last_seen_at,
        )
        if not written:
            return None

        await self._publish_snapshot_if_available(written[0], meta, data)
        return _wire_row(written[0])

    async def _publish_snapshot_if_available(
        self, row: dict[str, Any] | None, meta: MessageMeta, data: dict[str, Any]
    ) -> None:
        """Best-effort snapshot publish: a no-op unless this node declares a
        bus_backed exposure AND the write returned a real row."""
        if self._snapshot_exposure is None or row is None:
            return
        source_event_id = str(
            data.get("event_id") or data.get("correlation_id") or meta.fallback_id
        )
        await self.publish_snapshot_delta(
            self._snapshot_exposure,
            op="upsert",
            row=_wire_row(row),
            source_event_id=source_event_id,
            source_topic=meta.topic,
            source_partition=meta.partition,
            source_offset=meta.offset,
        )


__all__ = ["RuntimeErrorFingerprintProjectionWriter"]
