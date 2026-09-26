# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Projection writer for durable definition-of-done verdicts (OMN-18900).

Consumes ``onex.evt.omnimarket.dod-verify-completed.v1`` and writes one row
per verification run into ``omninode_internal.dod_verify_runs``.

THE TWO-CLASS SHAPE, AND WHY A PURE ENTRY ALONE WOULD SILENTLY WRITE NOTHING
    The runtime's projection wiring injects ``_db``, ``_event_type``,
    ``_topic`` and the envelope metadata into the bare event dict and then
    calls the entry EXPECTING IT TO WRITE. A definition-B entry handed that
    dict validates and returns: the projection consumes every message, commits
    its offsets and stores nothing, while consumer lag and every topic
    watermark read healthy. So the runtime-facing entry lives here and takes
    the injected dict, and the pure fold stays in
    ``HandlerProjectionDodVerdict`` where a unit test can still falsify the
    done predicate without a database.

    The fold logic is NOT duplicated here. This class instantiates the pure
    handler and calls it, so the SQL writer and the in-memory derivation
    cannot drift into disagreeing about whether a run counted as done.

WHY EVERY TERMINAL STATUS WRITES
    A failing verification writes a row, and so does an unresolved one. A
    verdict surface that exists only on success cannot distinguish a failure
    from a verification nobody ran, and telling those apart is the whole
    reason to have the surface -- the same argument the lab-pass receipt
    emitter records for emitting under ``always()``. There is no status filter
    anywhere in this module.

WHY THE KEY CARRIES THE COMPLETION TIME
    One row per RUN. The metric this table serves is attempts until the
    definition of done verifies true, so a re-verification of the same ticket
    is a new row rather than an overwrite: a table keyed on the ticket alone
    holds only the last answer and can never be asked how many attempts
    preceded it. A genuine redelivery carries the same three key components
    and the same event time, so it converges on the row it already wrote.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from omnimarket.nodes.node_projection_dod_verdict.handlers.handler_projection_dod_verdict import (
    HandlerProjectionDodVerdict,
)
from omnimarket.nodes.node_projection_dod_verdict.models import (
    ModelDodVerdictProjectionRequest,
    ModelDodVerdictProjectionResult,
    ModelDodVerdictRow,
    ModelDodVerdictWire,
)
from omnimarket.projection.runner import BaseProjectionRunner, MessageMeta

logger = logging.getLogger(__name__)

TABLE = "omninode_internal.dod_verify_runs"

# The report key the runtime reads for rows a writer DECLINED on purpose
# (omnibase_infra ``ROWS_REFUSED_KEY``, OMN-18992). A declined message is
# logged at INFO and counted as a refusal rather than as the "wrote zero rows"
# ERROR, which exists to surface a writer that silently stored nothing. The
# key is named for the first writer that needed it, an ordering guard; the
# runtime's meaning is "declined deliberately, no terminal owed", which is
# what a rehearsal is. Restated rather than imported: this module is loaded by
# processes that do not import the runtime's wiring package.
ROWS_REFUSED_KEY = "rows_refused_by_ordering_guard"

# A redelivery of the same run converges rather than duplicating or erroring.
# Every non-key column is re-asserted from EXCLUDED because the three key
# components pin the run completely: two payloads carrying the same ticket,
# correlation id and completion time ARE the same run, so there is no ordering
# question to guard here and no newer-wins predicate to get wrong.
#
# projected_at is the one wall-clock value on the row and it is deliberately
# not part of any verdict: it says when the reducer folded the event, never
# anything about the run.
_UPSERT = f"""
    INSERT INTO {TABLE} (
        ticket_id, correlation_id, completed_at, started_at,
        status, unresolved_cause,
        total_checks, verified_count, failed_count, skipped_count,
        superseded_count, non_probative_count, behavior_proving_count,
        readback_proving_count, unbindable_overlay_count,
        outcome, outcome_refusal, error_message, projected_at,
        delegation_correlation_id
    )
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14,
            $15, $16, $17, $18, $19, $20)
    ON CONFLICT (ticket_id, correlation_id, completed_at) DO UPDATE SET
        started_at = EXCLUDED.started_at,
        status = EXCLUDED.status,
        unresolved_cause = EXCLUDED.unresolved_cause,
        total_checks = EXCLUDED.total_checks,
        verified_count = EXCLUDED.verified_count,
        failed_count = EXCLUDED.failed_count,
        skipped_count = EXCLUDED.skipped_count,
        superseded_count = EXCLUDED.superseded_count,
        non_probative_count = EXCLUDED.non_probative_count,
        behavior_proving_count = EXCLUDED.behavior_proving_count,
        readback_proving_count = EXCLUDED.readback_proving_count,
        unbindable_overlay_count = EXCLUDED.unbindable_overlay_count,
        outcome = EXCLUDED.outcome,
        outcome_refusal = EXCLUDED.outcome_refusal,
        error_message = EXCLUDED.error_message,
        projected_at = EXCLUDED.projected_at,
        delegation_correlation_id = EXCLUDED.delegation_correlation_id
    RETURNING ticket_id, correlation_id, completed_at, outcome, outcome_refusal
"""


class DodVerdictProjectionWriter(BaseProjectionRunner):
    """Projects completed verifications into durable per-run rows.

    Named ``...Writer`` rather than ``...Runner``: ``Runner`` is a
    non-canonical type-word the OMN-14350 ratchet hard-fails and whose
    allowlist may only shrink, while a ``Handler``-prefixed class is required
    by the OMN-10821 wiring check to be importable from a Python module --
    which would drag the aiokafka projection-runner stack into the import path
    of every consumer of the pure handler. ``Writer`` is also the accurate
    word: this is the projection writer.
    """

    #: Dispatched IN-PROCESS by the runtime auto-wiring, once per consumed
    #: message, rather than driven by its own ``run()`` consume loop. The
    #: runtime reads this attribute BY NAME. Undeclared, a runner-shaped class
    #: -- one owning ``project_event``, ``run``, ``topics`` and its own
    #: adapter -- is classified STANDALONE, and the shared runtime subscribes
    #: its topics and then dispatches nothing. There is no dedicated writer
    #: Deployment for this node, so undeclared it would store nothing while
    #: reading healthy on lag and on every watermark.
    #:
    #: Declaring it is a promise the runtime cannot check: that every
    #: loop-bound resource this class touches is opened and closed inside the
    #: single loop ``handle()`` opens for one message. A pool cached across
    #: calls belongs to a loop that no longer exists, and reaching for it
    #: raises "Event loop is closed" on every message afterwards while offsets
    #: keep advancing.
    onex_runtime_inprocess_dispatch = True

    def __init__(self, contract_path: Path | None = None) -> None:
        super().__init__()
        _path = contract_path or Path(__file__).parent.parent / "contract.yaml"
        with open(_path) as handle:
            self._contract: dict[str, Any] = yaml.safe_load(handle)
        self._fold = HandlerProjectionDodVerdict()

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
        node can declare ``dlq_topics`` and still quarantine nothing -- the
        declaration reads as wired and is not.
        """
        return list(self._contract.get("event_bus", {}).get("dlq_topics", []))

    async def publish_dlq(self, topic: str, value: bytes) -> None:
        """OMN-13634: supply the runtime-owned publisher to the base DLQ path."""
        publish = await self.get_publish_fn()
        if publish is None:
            logger.error(
                "node_projection_dod_verdict: no publisher for POISON DLQ "
                "topic %s (verdict NOT quarantined)",
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
        """Project one runtime-dispatched message and report what was written."""
        # The fold runs before the pool opens: it is pure, and a verdict it
        # refuses to validate must not cost a connection.
        result = self._fold_verdict(data)
        declined = result.row is None
        await self.db.connect()
        try:
            written = None if result.row is None else await self._persist(result.row)
        finally:
            await self._stop_producer()
            await self.db.close()
        # ``rows_upserted`` is the key the runtime's own write-path guard
        # reads; it gates the terminal event on a PROVEN write and scores any
        # other shape zero. A writer returning, say, ``{"applied": True}`` has
        # every message logged as "Projection handler wrote zero rows" and its
        # terminal suppressed, including the messages that really did write.
        #
        # OMN-18901: a rehearsal the fold declined reports the refusal key as
        # well, so the runtime logs it as expected and its apply counter does
        # not read a deliberate skip as a write path that stored nothing.
        return {
            "rows_upserted": 0 if written is None else 1,
            ROWS_REFUSED_KEY: 1 if declined else 0,
            "dod_verdict_rows": [] if written is None else [written],
        }

    async def project_event(
        self, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> bool:
        """Standalone-runner entrypoint: project one message, report success.

        Boolean because :class:`BaseProjectionRunner`'s own consume loop is its
        caller and commits offsets on that answer.
        """
        await self._project_verdict(data)
        return True

    async def _project_verdict(self, data: dict[str, Any]) -> dict[str, Any] | None:
        """Fold and persist one verdict.

        Returns a compact description of the row the database accepted, or
        ``None`` when nothing was written. ``None`` is a real answer reported
        as such, never a truthy acknowledgement the runtime would read as a
        row. The in-process entry above calls the two halves itself, because
        it must also tell the runtime WHY nothing was written.
        """
        result = self._fold_verdict(data)
        if result.row is None:
            return None
        return await self._persist(result.row)

    def _fold_verdict(self, data: dict[str, Any]) -> ModelDodVerdictProjectionResult:
        """Validate the wire payload and run the pure fold over it.

        The fold, not this writer, decides whether an event is stored
        (OMN-18901): a rehearsal comes back with no row. Logged rather than
        silent, so a run that produced no row for a legitimate reason reads
        differently from one whose write path is broken.
        """
        event = ModelDodVerdictWire.model_validate(data)
        result = self._fold.handle(ModelDodVerdictProjectionRequest(event=event))
        if result.row is None:
            logger.info(
                "projection_dod_verdict: declined dry-run verdict for %s "
                "(correlation %s); rehearsals are not projected",
                event.ticket_id,
                event.correlation_id,
            )
        return result

    async def _persist(self, row: ModelDodVerdictRow) -> dict[str, Any] | None:
        """Upsert one folded row; describe what the database accepted."""
        written = await self.db.execute(
            _UPSERT,
            row.ticket_id,
            row.correlation_id,
            row.completed_at,
            row.started_at,
            row.status.value,
            None if row.unresolved_cause is None else row.unresolved_cause.value,
            row.total_checks,
            row.verified_count,
            row.failed_count,
            row.skipped_count,
            row.superseded_count,
            row.non_probative_count,
            row.behavior_proving_count,
            row.readback_proving_count,
            row.unbindable_overlay_count,
            row.outcome.value,
            None if row.outcome_refusal is None else row.outcome_refusal.value,
            row.error_message,
            datetime.now(UTC),
            # OMN-19514: the delegation run the verification judged, or NULL.
            row.delegation_correlation_id,
        )
        if not written:
            return None

        accepted = written[0]
        return {
            "ticket_id": str(accepted["ticket_id"]),
            "correlation_id": str(accepted["correlation_id"]),
            "completed_at": _isoformat(accepted["completed_at"]),
            "outcome": str(accepted["outcome"]),
            "outcome_refusal": (
                None
                if accepted["outcome_refusal"] is None
                else str(accepted["outcome_refusal"])
            ),
        }


def _isoformat(value: Any) -> str:
    """Render a returned timestamp for the terminal event's payload.

    Reduced to a string here rather than at the transport, where a value the
    encoder cannot handle would take the terminal event down with it.
    """
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


__all__ = ["DodVerdictProjectionWriter"]
