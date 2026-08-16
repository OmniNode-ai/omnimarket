# SPDX-License-Identifier: MIT
"""Hook-event capture: gateway command batch -> hook_events rows (OMN-16090).

Consumes the capture command topic declared in this node's ``contract.yaml``
(``event_bus.subscribe_topics``), validates the batch against
:class:`ModelHookEventCaptureRequest`, and writes one row per carried event,
idempotent on ``(tenant_id, event_sha)``.

No topic literal appears anywhere in this module -- ``topics`` and
``poison_dlq_topics`` both read the contract at call time. That is not only
ARCH-TOPIC-001 compliance: a literal here would be a second, ungoverned copy of
a name the contract already owns, and nothing would ever validate the two
against each other.

Three properties are load-bearing and each is enforced rather than assumed:

1.  **Idempotency is the database's job, not the runner's.** A redelivered
    Kafka batch (rebalance, offset replay, a retried submission) must be a
    no-op, and the only place that can be guaranteed is the UNIQUE constraint.
    The writer uses ``ON CONFLICT ... DO NOTHING`` and reports how many rows
    were actually new, so a replay is observable rather than invisible.

2.  **The tenant key is the immutable principal.** ``payload.tenant_id`` is
    attribution-only on this wire (onex-api says so explicitly: the forwarder
    path overwrites it, and the direct-lane consumer falls back to
    ``ONEX_TENANT_ID``). Keying tenant-isolated rows on it would silently merge
    or split two tenants' history. The model requires ``tenant_principal_id``.

3.  **A malformed batch is POISON, not a retry.** A batch that fails model
    validation will fail identically on every redelivery. Returning False would
    spin the consumer forever on the same offset; raising the classified poison
    path routes it to the DLQ once.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_hook_event_capture.models.model_hook_event_capture_request import (
    ModelHookEventCaptureRequest,
)
from omnimarket.projection.runner import (
    BaseProjectionRunner,
    MessageMeta,
    safe_parse_date,
)
from omnimarket.projection.tenant_isolation import house_tenant_write_stamp

logger = logging.getLogger(__name__)

TABLE = "hook_events"
_CONTRACT_PATH = Path(__file__).resolve().parent.parent / "contract.yaml"


def _contract_topics(key: str) -> list[str]:
    """Read a topic list from THIS node's contract.

    Topics are read from the contract rather than restated as literals here:
    the contract is the declaration the platform's topic gates and the gateway
    catalog are checked against, so a second copy in Python is a drift surface
    with no upside.
    """
    data: dict[str, Any] = yaml.safe_load(_CONTRACT_PATH.read_text())
    bus: dict[str, Any] = data.get("event_bus") or {}
    topics: list[str] = list(bus.get(key) or [])
    return topics


class HookEventCaptureError(Exception):
    """Malformed capture batch. Classified POISON: retrying cannot help."""


class HandlerHookEventCapture(BaseProjectionRunner):
    """Persists gateway-submitted hook-event batches into ``hook_events``."""

    @property
    def topics(self) -> list[str]:
        return _contract_topics("subscribe_topics")

    @property
    def poison_dlq_topics(self) -> list[str]:
        # No DLQ topic is declared for this node yet, so the base class's
        # fail-loud behaviour applies: an unroutable poison event re-raises
        # rather than being silently dropped. Declaring a DLQ topic here
        # without provisioning it on the broker would convert a loud failure
        # into a silent one, which is the wrong direction.
        return _contract_topics("dlq_topics")

    def handle(self, input_data: dict[str, Any]) -> dict[str, Any]:
        """RuntimeLocal handler protocol shim (canonical definition-B entrypoint).

        Without this, auto-wiring binds the handler to ``_missing_handle`` and
        EVERY dispatch raises at runtime while CI stays green -- the failure
        mode ``tests/validators/test_handler_dispatch_entrypoint.py`` exists to
        catch. Same shape as the sibling projection runners: pop the transport
        coordinates the runtime threads in under reserved ``_``-prefixed keys,
        then delegate to the one real implementation in ``project_event``.

        The coordinates are POPPED, not read: they are transport metadata, not
        contract fields, and ``ModelHookEventCaptureRequest`` is
        ``extra="forbid"`` -- leaving them in the dict would make every
        RuntimeLocal dispatch fail validation as a malformed batch.
        """
        topics = self.topics
        topic = str(input_data.pop("_topic", topics[0] if topics else ""))
        meta = MessageMeta(
            partition=int(input_data.pop("_partition", 0)),
            offset=int(input_data.pop("_offset", 0)),
            fallback_id=str(input_data.pop("_fallback_id", "")),
            topic=topic,
        )
        captured = asyncio.run(self.project_event(topic, input_data, meta))
        return {"captured": captured}

    async def project_event(
        self, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> bool:
        """Persist every event in one batch. Returns False only on DB trouble."""
        try:
            batch = ModelHookEventCaptureRequest.model_validate(data)
        except ValidationError as exc:
            # POISON, not a retry: identical redelivery fails identically.
            raise HookEventCaptureError(
                f"malformed hook-event-capture batch on {topic} "
                f"(partition={meta.partition} offset={meta.offset}): {exc}"
            ) from exc

        # The stored tenant value must equal what the database will hold, so an
        # RLS WITH CHECK evaluated by a non-superuser writer has something true
        # to compare against (OMN-15301). The helper fails closed when
        # ENFORCE_TENANT_ISOLATION flips and no tenant can be resolved.
        tenant_stamp = house_tenant_write_stamp(table=TABLE)
        tenant_id = tenant_stamp.get("tenant_id", "omninode")

        inserted = await self._insert_batch(batch, tenant_id)
        duplicates = len(batch.events) - inserted
        await self._publish_terminal(batch, inserted, duplicates)
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
        return True

    async def _publish_terminal(
        self, batch: ModelHookEventCaptureRequest, inserted: int, duplicates: int
    ) -> bool:
        """Emit ONE batch-completion event. Best-effort by design.

        This is what lets the gateway's workflow_terminal_consumer move
        GET /v1/workflows/{id}/status from `published` to `completed`, which is
        the only signal a submitter can trust: the gateway's publish is
        fail-open, so a 202 does not mean the batch landed, and the operator
        shipper refuses to move spool files aside without a terminal status.

        Best-effort, and deliberately so: the rows are already committed when
        this runs. Failing the message here would re-deliver a batch that has
        already been persisted -- harmless for the rows (the unique constraint
        absorbs it) but it would stall the partition on a transport problem
        that has nothing to do with the data. A missing terminal event degrades
        confirmation, it does not corrupt capture; the error is logged loudly
        rather than swallowed silently.

        NOT a re-emission of the captured events onto their original topics.
        One event, carrying counts and the batch identity.
        """
        topics = _contract_topics("publish_topics")
        if not topics:
            return False
        publish = await self.get_publish_fn()
        if publish is None:
            # No broker configured (unit/CLI contexts). Not an error.
            return False
        payload = {
            "event_type": "omnimarket.hook-events-captured",
            "batch_sha": batch.batch_sha,
            "source": batch.source,
            "correlation_id": batch.correlation_id,
            "tenant_principal_id": batch.tenant_principal_id,
            "events_received": len(batch.events),
            "events_persisted": inserted,
            "events_already_present": duplicates,
        }
        try:
            await publish(topics[0], json.dumps(payload).encode("utf-8"))
        except Exception:
            logger.exception(
                "hook-event-capture: batch %s persisted %d/%d event(s) but the "
                "terminal event could not be published to %s. The rows are "
                "committed; the submitter will not see a terminal status and "
                "will leave its spool files in place, which is the safe "
                "direction.",
                batch.batch_sha[:12],
                inserted,
                len(batch.events),
                topics[0],
            )
            return False
        return True

    async def _insert_batch(
        self, batch: ModelHookEventCaptureRequest, tenant_id: str
    ) -> int:
        """Insert every event in one statement. Returns the count of NEW rows.

        One statement, not a loop. A 250-event batch as 250 round trips would
        also be 250 separate transactions, so a mid-batch failure would leave
        the batch half-applied with the offset un-committed; the next delivery
        would re-insert the surviving half and lean entirely on the unique
        constraint to stay correct. A single statement makes the batch atomic:
        either the whole batch lands or none of it does.

        ``ON CONFLICT DO NOTHING`` rather than ``DO UPDATE``: a captured event
        is immutable history. Re-receiving the same event_sha means the same
        bytes arrived twice, so there is nothing to update -- and an UPDATE
        would move ``updated_at``, making a pure replay look like new activity
        to anything reading that column.

        ``tenant=`` is passed explicitly (OMN-15919). Leaving it None makes the
        adapter stamp the RLS GUC from its own READ-path resolver, which can
        differ from the tenant the row actually carries -- the exact
        two-resolver split that produced "new row violates row-level security
        policy" on every delegation write whose row tenant differed from the
        read-path default.
        """
        occurred_at: list[datetime] = [
            safe_parse_date(e.occurred_at) for e in batch.events
        ]
        spooled_at: list[datetime | None] = [
            safe_parse_date(e.spooled_at) if e.spooled_at else None
            for e in batch.events
        ]
        rows = await self.db.execute(
            f"""
            INSERT INTO {TABLE} (
                tenant_id, event_sha, event_type, occurred_at, payload,
                event_id, correlation_id, run_id,
                source, batch_sha, spooled_at, spool_reason
            )
            SELECT
                $1,
                src.event_sha, src.event_type, src.occurred_at,
                src.payload_json::jsonb,
                src.event_id, src.correlation_id, src.run_id,
                $2, $3, src.spooled_at, src.spool_reason
            FROM unnest(
                $4::text[], $5::text[], $6::timestamptz[], $7::text[],
                $8::text[], $9::text[], $10::text[],
                $11::timestamptz[], $12::text[]
            ) AS src(
                event_sha, event_type, occurred_at, payload_json,
                event_id, correlation_id, run_id,
                spooled_at, spool_reason
            )
            ON CONFLICT (tenant_id, event_sha) DO NOTHING
            RETURNING id
            """,
            tenant_id,
            batch.source,
            batch.batch_sha,
            [e.event_sha for e in batch.events],
            [e.event_type for e in batch.events],
            occurred_at,
            [e.payload_json for e in batch.events],
            [e.event_id for e in batch.events],
            [e.correlation_id for e in batch.events],
            [e.run_id for e in batch.events],
            spooled_at,
            [e.spool_reason for e in batch.events],
            tenant=tenant_id,
        )
        return len(rows)
