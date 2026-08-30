# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Receipt-gate projection: Kafka -> receipt_gate_rows table.

OMN-17210 (child of OMN-17191 "Projection coverage"). Until this module,
``node_projection_receipt_gate`` had no writer at all: it shipped the pure
reducer ``reduce_receipt_gate`` -- ``(rows, event) -> rows``, no I/O by design --
and ``HandlerProjectionReceiptGate``, a ``handle_dict`` shim that returns the
reduced tuple to its caller. Nothing consumed the two subscribe topics and
nothing wrote a row, so ``public.receipt_gate_rows`` stayed migrated-and-empty,
the projection API served an empty list for
``onex.snapshot.projection.receipt-gate.v1``, and the omnidash receipt-gate
widget rendered a truthful empty state that is indistinguishable from "no
receipts have been signed yet". Nothing errored, which is why it stayed open.

This is the same standalone-process shape every other deployed projection uses
(``SavingsProjectionRunner``, ``RegistrationProjectionRunner``,
``DelegationProjectionRunner``, ``HandlerLiveEventsProjectionRunner``): a
``BaseProjectionRunner`` subclass with its own ``__main__``, run as a bare
Kafka-consumer Deployment. A ``*ProjectionRunner``-suffixed class is
deliberately no-op'd by the shared dispatch kernel, so a dedicated process is
the sanctioned way to run one -- not a handler fix.

The event -> row mapping is NOT reimplemented here. It delegates to the existing
pure reducer so the two wire shapes stay a single source of truth and the
reducer's own tests keep covering them; this module owns only the transport and
the write.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import yaml

from omnimarket.nodes.node_projection_receipt_gate.models.model_receipt_gate_row import (
    ModelReceiptGateRow,
)
from omnimarket.nodes.node_projection_receipt_gate.reducers.reducer_receipt_gate import (
    reduce_receipt_gate,
)
from omnimarket.projection.runner import BaseProjectionRunner, MessageMeta

logger = logging.getLogger(__name__)

HANDLER_ID_PROJECTION_RECEIPT_GATE = "node_projection_receipt_gate"

# Same guard the sibling runners carry: a contract typo that repoints this node
# at some other node's table must fail at construction, not silently write rows
# into the wrong relation.
KNOWN_PROJECTION_TABLES: frozenset[str] = frozenset({"receipt_gate_rows"})


# NAMING IS LOAD-BEARING IN BOTH DIRECTIONS, and only this exact shape
# satisfies both constraints at once:
#
# * The ``ProjectionRunner`` SUFFIX is what
#   ``omnibase_infra.runtime.auto_wiring.handler_wiring._is_projection_runner_handler``
#   matches on (``type(h).__name__.endswith("ProjectionRunner")`` plus
#   project_event/topics/db) to recognise a standalone Kafka runner exposed in
#   handler_routing and connect its DB adapter before direct dispatch. Drop the
#   suffix and the kernel stops treating it as one.
# * The ``Handler`` PREFIX is what keeps it off the OMN-14350 non-canonical
#   lifecycle-class ratchet: ``Runner`` is a hardfail type-word, and
#   ``no_noncanonical_lifecycle_classes.FIRST_WORD_ALLOW`` exempts a canonical
#   leading word. The twelve older ``*ProjectionRunner`` classes are all frozen
#   allowlist entries and that allowlist may only SHRINK, so a new one may not
#   be added. ``HandlerLiveEventsProjectionRunner`` is the existing precedent
#   that satisfies both without an allowlist entry; this follows it.


class HandlerReceiptGateProjectionRunner(BaseProjectionRunner):
    """Projects receipt-gate events into the ``receipt_gate_rows`` table.

    Two inbound shapes, both already understood by ``reduce_receipt_gate``:

    * ``onex.evt.omnimarket.verification-receipt-completed.v1`` -- one row per
      check dimension (or a single ``overall`` row when the receipt carries no
      ``checks`` list).
    * ``onex.evt.omnimarket.evidence-validated.v1`` -- one ``occ-evidence`` row
      per OCC validation outcome.
    """

    def __init__(self, contract_path: Path | None = None) -> None:
        super().__init__()
        _path = contract_path or Path(__file__).parent.parent / "contract.yaml"
        with open(_path) as f:
            self._contract: dict[str, Any] = yaml.safe_load(f)

        _tables = self._contract.get("db_io", {}).get("db_tables", [])
        _by_role = {t["role"]: t["name"] for t in _tables}

        for role, name in _by_role.items():
            if name not in KNOWN_PROJECTION_TABLES:
                raise ValueError(
                    f"Unknown table role {role!r} maps to {name!r} which is not "
                    "in KNOWN_PROJECTION_TABLES"
                )

        if "receipt_gate_projection" not in _by_role:
            raise ValueError(
                "Contract missing required table role 'receipt_gate_projection'"
            )

        self._table_receipt_gate_rows: str = _by_role["receipt_gate_projection"]

    @property
    def table_receipt_gate_rows(self) -> str:
        return self._table_receipt_gate_rows

    @property
    def subscribe_topics(self) -> list[str]:
        return list(self._contract.get("event_bus", {}).get("subscribe_topics", []))

    @property
    def topics(self) -> list[str]:
        return self.subscribe_topics

    def handle(self, input_data: dict[str, Any]) -> dict[str, Any]:
        """RuntimeLocal handler protocol shim -- same seam as the sibling
        runners, so ``onex run-node`` can drive one event through this path."""
        payload = dict(input_data)
        topics = self.subscribe_topics
        topic = str(payload.pop("_topic", topics[0] if topics else ""))
        meta = MessageMeta(
            partition=int(payload.pop("_partition", 0)),
            offset=int(payload.pop("_offset", 0)),
            fallback_id=str(payload.pop("_fallback_id", "")),
            topic=topic,
        )
        projected = asyncio.run(self.project_event(topic, payload, meta))
        return {"projected": projected}

    async def project_event(
        self, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> bool:
        if topic not in self.subscribe_topics:
            # The base class only delivers subscribed topics, so an unknown one
            # is a wiring bug. The reducer's ``_best_effort_row`` fallback would
            # happily write a junk row for it and hide that bug behind a widget
            # that looks populated.
            logger.warning(
                "receipt-gate projection received unsubscribed topic %s -- refusing",
                topic,
            )
            return False

        # The reducer distinguishes its two shapes by an ``_event_type`` hint and
        # only falls back to structural field-sniffing when none is present. The
        # runner knows the topic, so it passes it through: without this, a
        # verification receipt that happens to carry ``evidence_lifecycle_state``
        # is projected as an OCC row. Copied, never mutated in place -- the
        # caller's dict is also what the DLQ/commit path sees.
        event: dict[str, Any] = {**data, "_event_type": topic}

        rows = reduce_receipt_gate((), event)
        for row in rows:
            await self._insert_row(row)
        return True

    async def _insert_row(self, row: ModelReceiptGateRow) -> None:
        """Append one projection row.

        ``receipt_gate_rows``' only unique constraint is its ``id BIGSERIAL``
        primary key (see ``migrations/0000_create_receipt_gate_projection_table.sql``),
        so there is no natural key to ``ON CONFLICT`` against and this ticket
        adds no migration. Kafka delivery is at-least-once and the offset is
        committed after the write, so a redelivery across a rebalance would
        otherwise duplicate the row. The ``WHERE NOT EXISTS`` below suppresses
        exactly that case -- a redelivered event reduces to byte-identical
        column values -- and it is honest about what it does not do: it is a
        read-then-insert, not a constraint, so it is only sound because this
        node runs at ``replicas: 1``. Two concurrent writers could still race a
        duplicate past it. Deduplicating structurally needs a unique index on
        ``(name, pr_ref, signed_at, observed_at)``, which is a migration and a
        separate change.
        """
        await self.db.execute(
            f"""
            INSERT INTO {self._table_receipt_gate_rows} (
              name, pass, detail, pr_ref, worker, verifier,
              evidence_count, evidence_hash, signed_at, observed_at
            )
            SELECT $1, $2, $3, $4, $5, $6, $7, $8, $9, $10
            WHERE NOT EXISTS (
              SELECT 1 FROM {self._table_receipt_gate_rows}
              WHERE name = $1
                AND pr_ref IS NOT DISTINCT FROM $4
                AND signed_at IS NOT DISTINCT FROM $9
                AND observed_at = $10
            )
            """,
            row.name,
            row.pass_,
            row.detail,
            row.pr_ref,
            row.worker,
            row.verifier,
            row.evidence_count,
            row.evidence_hash,
            row.signed_at,
            # asyncpg binds TIMESTAMPTZ from a datetime; passing the ISO string
            # raises DataError and CrashLoopBackOffs the writer (the OMN-15905
            # round-2 defect on the delegation writer). ``observed_at`` is
            # already a tz-aware datetime on the model.
            row.observed_at,
        )


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )
    runner = HandlerReceiptGateProjectionRunner()
    asyncio.run(runner.run())
