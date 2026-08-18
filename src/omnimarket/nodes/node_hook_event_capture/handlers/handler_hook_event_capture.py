# SPDX-License-Identifier: MIT
"""Hook-event capture: gateway command batch -> hook_events rows (OMN-16090).

DISPATCH SHAPE (OMN-16090 fix). The contract declares ``db_io.db_tables``, and
``omnibase_infra.runtime.auto_wiring.handler_wiring.wire_from_manifest``
branches on that BEFORE it ever looks at ``handler_routing``/``event_model``
(``if db_tables: ... callback = _make_projection_dispatch_callback(...)`` —
unconditional). That projection arm builds a PLAIN ``dict`` — the payload's
``model_dump(mode="json")`` plus injected ``_db`` / ``_event_type`` / ``_topic``
keys — and calls ``handler_instance.handle(input_data)``. It never calls
``project_event()`` and it never passes a validated model.

This node originally shipped ``handle(self, request: ModelHookEventCaptureRequest)``.
That signature satisfied the OMN-14355 canon-shape ratchet's STATIC check (a
parameter literally named ``request`` classifies canonical regardless of its
type annotation — see ``MAGIC_PARAM_NAMES`` in
``omnibase_core/scripts/ci/canonical_handler_shape.py``) but never actually ran
on the real dispatch path: every live call passed a dict, and
``request.batch_sha`` raised ``AttributeError``. The arm's generic
``except Exception`` swallowed it, logged it, and routed the raw envelope to
platform quarantine (no ``dlq_topics`` declared here) — a silent
consume-then-drop: the Kafka offset commits, consumer-group lag reads 0, and
zero rows land. Same class as the OMN-13825 incident documented in
``omnimarket/projection/handler_shim.py``.

The fix keeps the parameter name ``request`` (still magic, still canonical per
the ratchet) but accepts the shape the runtime actually sends: a mapping
carrying the injected metadata keys alongside the batch payload. Injected-key
extraction routes through the canonical :func:`split_projection_input` helper
(``omnimarket.projection.handler_shim``) so this handler does not hand-roll
``.pop()`` calls — the pattern every other ``db_io`` projection handler in
this repo (``node_projection_dep_health``, ``node_projection_overnight``, ...)
already follows.

WHY THIS CLASS NO LONGER EXTENDS ``BaseProjectionRunner``. The previous
revision subclassed it and implemented ``project_event()`` as a second,
parallel entrypoint. ``BaseProjectionRunner.__init__`` builds its OWN
``AsyncpgAdapter`` from ``ModelProjectionRuntimeBinding.from_legacy_settings()``
— a topology-BLIND DB connection, entirely disconnected from the
topology-resolved ``DatabaseAdapter`` the projection dispatch arm actually
injects at ``input_data['_db']``. Nothing in the deployed topology constructs
a standalone ``BaseProjectionRunner`` process for THIS node — auto-wiring
subscribes the node itself via the projection arm; there is no dedicated
Deployment, unlike e.g. ``HandlerLiveEventsProjectionRunner``, which onex-dev
does run standalone. So ``project_event()`` was dead on the only path that
actually dispatches events to this handler. Keeping it as "dead but harmless"
would not have been harmless: a future standalone Deployment wired against
``project_event()`` would silently write through the wrong DB resolution,
diverging from every other ``db_io`` node's topology-routed adapter, and the
class would keep carrying two contradictory persistence implementations with
no test proving either matches the deployed reality. Deleted, not parked.

IDEMPOTENCY, AND WHY IT IS QUERY-THEN-UPSERT INSTEAD OF ONE UNNEST STATEMENT.
The injected ``DatabaseAdapter`` (``omnibase_infra`` `ProjectionDatabaseOperations`,
matching the local ``omnimarket.projection.protocol_database.DatabaseAdapter``
protocol) exposes only ``upsert(table, conflict_key, row) -> bool`` and
``query(table, filters) -> list[dict]`` — one row at a time, no raw multi-row
SQL. Its ``upsert()`` also has no ``ON CONFLICT ... DO NOTHING`` mode: when the
row carries any column besides the conflict key, it always builds
``ON CONFLICT (...) DO UPDATE SET ...`` (see
``ProjectionDatabaseOperations._execute_upsert``). ``hook_events`` has a
``BEFORE UPDATE`` trigger that bumps ``updated_at`` — so an unconditional
``upsert()`` on every redelivered event would touch ``updated_at`` on a pure
replay, contradicting "captured events are immutable history" (the exact
reason the ORIGINAL raw-SQL design chose ``DO NOTHING`` over ``DO UPDATE``).
This handler queries for an existing ``(tenant_id, event_sha)`` row BEFORE
writing and skips the upsert when found, so the realistic Kafka-redelivery
case — the same consumer re-processing an already-committed batch — never
touches ``updated_at``, and ``events_persisted`` / ``events_already_present``
stay accurate. Trade-off: up to 2N round trips per batch instead of one atomic
statement — a real change from the original design, forced by the injected
adapter's single-row surface. A genuine concurrent double-delivery (two
processes upserting the same never-before-seen event at once) can still race
through to the ``DO UPDATE`` fallback; that residual is pre-existing to every
``DatabaseAdapter``-backed projection handler in this repo, not something this
fix introduces.

TERMINAL EVENT. The contract declares ``terminal_event`` in ``publish_topics``,
so the projection dispatch arm emits it itself (gated on the returned dict's
``rows_upserted`` >= 1 via ``_extract_rows_upserted``) once ``handle()``
returns — matching ``node_projection_overnight`` and every other ``db_io``
node with a declared terminal event. This handler no longer publishes its own
terminal event; the previous manual ``_publish_terminal``/``get_publish_fn``
machinery is gone with ``BaseProjectionRunner``.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping

from pydantic import ValidationError

from omnimarket.nodes.node_hook_event_capture.models.model_hook_event_capture_request import (
    ModelHookEventCaptureRequest,
    ModelHookEventCaptureResult,
)
from omnimarket.projection.handler_shim import split_projection_input
from omnimarket.projection.protocol_database import DatabaseAdapter
from omnimarket.projection.tenant_isolation import house_tenant_write_stamp

logger = logging.getLogger(__name__)

TABLE = "hook_events"
CONFLICT_KEY = "tenant_id,event_sha"


class HookEventCaptureError(Exception):
    """Malformed capture batch. Classified POISON: retrying cannot help."""


class HandlerHookEventCapture:
    """Persists gateway-submitted hook-event batches into ``hook_events``."""

    def handle(self, request: object) -> dict[str, object]:
        """Canonical definition-B entrypoint the runtime auto-wiring binds.

        ``request`` carries the runtime-injected metadata (``_db``,
        ``_event_type``, ``_topic``) alongside the batch payload — see the
        module docstring for why this is a mapping rather than the typed
        ``ModelHookEventCaptureRequest`` a naive def-B read would expect.
        """
        if not isinstance(request, Mapping):
            raise TypeError(
                "HandlerHookEventCapture.handle() expects the runtime-injected "
                f"payload mapping (with _db/_event_type/_topic), got "
                f"{type(request).__name__}"
            )
        db, payload, _meta = split_projection_input(dict(request))
        try:
            batch = ModelHookEventCaptureRequest.model_validate(payload)
        except ValidationError as exc:
            # POISON, not a retry: identical redelivery fails identically. A
            # plain ValidationError would already route to the contract DLQ
            # via the arm's generic except-Exception handler; wrapping it
            # keeps this handler's own error type stable for callers/tests.
            raise HookEventCaptureError(
                f"malformed hook-event-capture batch: {exc}"
            ) from exc
        result = self._capture(batch, db)
        payload_out: dict[str, object] = result.model_dump(mode="json")
        # OMN-13360: the arm gates its own terminal-event emission on
        # rows_upserted >= 1 (see _extract_rows_upserted); events_persisted is
        # this handler's exact same count under a different, node-local name.
        payload_out["rows_upserted"] = result.events_persisted
        return payload_out

    def _capture(
        self, batch: ModelHookEventCaptureRequest, db: DatabaseAdapter
    ) -> ModelHookEventCaptureResult:
        """The one real implementation."""
        # The stamped tenant value must equal what the database will hold, so
        # an RLS WITH CHECK evaluated by a non-superuser writer has something
        # true to compare against (OMN-15301). The helper fails closed when
        # ENFORCE_TENANT_ISOLATION flips and no tenant can be resolved.
        tenant_stamp = house_tenant_write_stamp(table=TABLE)
        tenant_id = tenant_stamp.get("tenant_id", "omninode")

        inserted = self._insert_batch(batch, tenant_id, db)
        duplicates = len(batch.events) - inserted
        logger.info(
            "hook-event-capture batch %s: %d event(s), %d new, %d already present "
            "(source=%s tenant=%s principal=%s)",
            batch.batch_sha[:12],
            len(batch.events),
            inserted,
            duplicates,
            batch.source,
            tenant_id,
            batch.tenant_principal_id,
        )
        return ModelHookEventCaptureResult(
            batch_sha=batch.batch_sha,
            events_received=len(batch.events),
            events_persisted=inserted,
            events_already_present=duplicates,
        )

    def _insert_batch(
        self,
        batch: ModelHookEventCaptureRequest,
        tenant_id: str,
        db: DatabaseAdapter,
    ) -> int:
        """Write every NEW event in the batch. Returns the count of NEW rows.

        Queries for an existing ``(tenant_id, event_sha)`` row first and skips
        the write when found — see the module docstring for why: the injected
        adapter's ``upsert()`` has no ``DO NOTHING`` mode, and an unconditional
        upsert would bump ``hook_events.updated_at`` on every replay.
        """
        inserted = 0
        for event in batch.events:
            existing = db.query(
                TABLE, {"tenant_id": tenant_id, "event_sha": event.event_sha}
            )
            if existing:
                continue
            row: dict[str, object] = {
                "tenant_id": tenant_id,
                "event_sha": event.event_sha,
                "event_type": event.event_type,
                "occurred_at": event.occurred_at,
                "payload": event.payload(),
                "event_id": event.event_id,
                "correlation_id": event.correlation_id,
                "run_id": event.run_id,
                "source": batch.source,
                "batch_sha": batch.batch_sha,
                "spooled_at": event.spooled_at,
                "spool_reason": event.spool_reason,
            }
            db.upsert(TABLE, CONFLICT_KEY, row)
            inserted += 1
        return inserted


__all__: list[str] = [
    "CONFLICT_KEY",
    "TABLE",
    "HandlerHookEventCapture",
    "HookEventCaptureError",
]
