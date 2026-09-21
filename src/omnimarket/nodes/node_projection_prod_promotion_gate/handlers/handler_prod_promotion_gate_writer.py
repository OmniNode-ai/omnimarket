# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Projection writer for prod-promotion-gate decisions (OMN-18999).

Consumes ``onex.evt.omnimarket.prod-promotion-gate-evaluated.v1`` and writes
one row per gate evaluation into
``omninode_internal.prod_promotion_gate_decisions``.

WHY THIS NODE EXISTS
    The gate is correct and its refusals are precise. Every one of them was a
    RETURN VALUE: the typed reason rode one bus event, was re-wrapped onto a
    second by the redeploy orchestrator, and no contract in the repository
    subscribed to either. A blocked promotion was therefore indistinguishable
    from a promotion nobody requested -- there was no row to query, so "why
    did this not promote" had no answer surface at all. On 2026-09-20 the lab
    lane sat frozen for two and a quarter hours behind six consecutive correct
    refusals for exactly this reason.

    The positive control for the mechanism is in this same repository:
    ``onex.evt.omnibase-infra.delegation-failed.v1`` has three subscribing
    projection contracts. Nothing about projecting a refusal is new. This
    reason simply was not projected.

WHY THE ALLOW PATH WRITES TOO
    There is no ``allowed`` filter in this module and none in the relation. A
    surface that records only refusals cannot tell an allowed promotion from a
    gate that never ran, and both then look like an empty result. With a row
    on every evaluation the two are a boolean apart.

THE TWO-CLASS SHAPE
    The runtime's projection wiring injects ``_db``, ``_topic`` and the
    envelope coordinates into the bare event dict and calls the entry
    EXPECTING IT TO WRITE. A pure definition-B entry handed that dict
    validates and returns: the projection consumes every message, commits its
    offsets and stores nothing, while consumer lag and every watermark read
    healthy. So the runtime-facing entry lives here and the pure fold stays in
    ``HandlerProjectionProdPromotionGate``, which this class instantiates and
    calls rather than reimplementing -- the writer and the fold cannot drift
    into disagreeing about what the decision said.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from omnimarket.nodes.node_projection_prod_promotion_gate.handlers.handler_projection_prod_promotion_gate import (
    HandlerProjectionProdPromotionGate,
)
from omnimarket.nodes.node_projection_prod_promotion_gate.models import (
    ModelProdPromotionGateDecisionWire,
    ModelProdPromotionGateProjectionRequest,
)
from omnimarket.projection.discovery import load_projection_exposures_from_contract
from omnimarket.projection.models import ProjectionTableConfig
from omnimarket.projection.runner import (
    BaseProjectionRunner,
    MessageMeta,
    deterministic_correlation_id,
)

logger = logging.getLogger(__name__)

TABLE = "omninode_internal.prod_promotion_gate_decisions"

# A redelivery converges rather than duplicating. The correlation IS the run,
# and a run's gate decision is one fact, so every non-key column is re-asserted
# from EXCLUDED: a second delivery of the same evaluation is the same answer,
# and a genuine re-evaluation under the same correlation is a corrected answer
# that should replace the stale one rather than sit beside it.
_UPSERT = f"""
    INSERT INTO {TABLE} (
        correlation_id, outcome, allowed, reason, grant_id,
        requested_image_digest, resolved_image_digest, rollback_target,
        runtime_lane, promotion_batch_id, evaluated_at, source_topic,
        projected_at
    )
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
    ON CONFLICT (correlation_id) DO UPDATE SET
        outcome = EXCLUDED.outcome,
        allowed = EXCLUDED.allowed,
        reason = EXCLUDED.reason,
        grant_id = EXCLUDED.grant_id,
        requested_image_digest = EXCLUDED.requested_image_digest,
        resolved_image_digest = EXCLUDED.resolved_image_digest,
        rollback_target = EXCLUDED.rollback_target,
        runtime_lane = EXCLUDED.runtime_lane,
        promotion_batch_id = EXCLUDED.promotion_batch_id,
        evaluated_at = EXCLUDED.evaluated_at,
        source_topic = EXCLUDED.source_topic,
        projected_at = EXCLUDED.projected_at
    RETURNING correlation_id, outcome, allowed, grant_id,
              requested_image_digest, evaluated_at
"""


class ProdPromotionGateProjectionWriter(BaseProjectionRunner):
    """Projects prod-promotion-gate decisions into durable rows.

    Named ``...Writer`` rather than ``...Runner``: ``Runner`` is a
    non-canonical type-word the OMN-14350 ratchet hard-fails, and a
    ``Handler``-prefixed class would be required by the OMN-10821 wiring check
    to be importable from a Python module, which would drag the aiokafka
    runner stack into the import path of every consumer of the pure fold.
    """

    #: Dispatched IN-PROCESS by the runtime auto-wiring, once per consumed
    #: message, rather than driven by its own ``run()`` loop. The runtime reads
    #: this attribute BY NAME; undeclared, a runner-shaped class is classified
    #: STANDALONE and the shared runtime subscribes its topics and dispatches
    #: nothing -- storing nothing while reading healthy on lag and on every
    #: watermark. There is no dedicated writer Deployment for this node.
    onex_runtime_inprocess_dispatch = True

    def __init__(self, contract_path: Path | None = None) -> None:
        super().__init__()
        _path = contract_path or Path(__file__).parent.parent / "contract.yaml"
        with open(_path) as handle:
            self._contract: dict[str, Any] = yaml.safe_load(handle)
        self._fold = HandlerProjectionProdPromotionGate()

        # OMN-15800: resolve this node's own bus_backed exposure from its own
        # already-loaded contract, so the serving path is contract-driven and
        # never a per-call decision in this class.
        node_name = str(self._contract.get("name", "projection_prod_promotion_gate"))
        exposures = load_projection_exposures_from_contract(
            self._contract, node_name, _path
        )
        self._snapshot_exposure: ProjectionTableConfig | None = next(
            (exposure for exposure in exposures if exposure.bus_backed), None
        )

    @property
    def subscribe_topics(self) -> list[str]:
        return list(self._contract.get("event_bus", {}).get("subscribe_topics", []))

    @property
    def topics(self) -> list[str]:
        return self.subscribe_topics

    @property
    def poison_dlq_topics(self) -> list[str]:
        """The contract-declared DLQ, READ FROM THE CONTRACT (OMN-13634).

        Read rather than restated so the declaration and the behaviour cannot
        diverge: the base class defaults this to an empty list, so a node can
        declare ``dlq_topics`` and still quarantine nothing.
        """
        return list(self._contract.get("event_bus", {}).get("dlq_topics", []))

    async def publish_dlq(self, topic: str, value: bytes) -> None:
        """Supply the runtime-owned publisher to the base DLQ path (OMN-13634)."""
        publish = await self.get_publish_fn()
        if publish is None:
            logger.error(
                "node_projection_prod_promotion_gate: no publisher for POISON "
                "DLQ topic %s (gate decision NOT quarantined)",
                topic,
            )
            return
        await publish(topic, value)

    def handle(self, input_data: dict[str, Any]) -> dict[str, Any]:
        """RuntimeLocal handler protocol shim: one message, one loop, one pool."""
        topics = self.subscribe_topics
        topic = str(input_data.pop("_topic", topics[0] if topics else ""))
        partition = int(input_data.pop("_partition", 0))
        offset = int(input_data.pop("_offset", 0))
        fallback = str(input_data.pop("_fallback_id", "")) or (
            deterministic_correlation_id(topic, partition, offset)
        )
        meta = MessageMeta(
            partition=partition,
            offset=offset,
            fallback_id=fallback,
            topic=topic,
        )
        return asyncio.run(self._project_one_message(topic, input_data, meta))

    async def _project_one_message(
        self, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> dict[str, Any]:
        """Project one runtime-dispatched message and report what was written."""
        await self.db.connect()
        try:
            written = await self._project_decision(data, meta)
        finally:
            await self._stop_producer()
            await self.db.close()
        # ``rows_upserted`` is the key the runtime's write-path guard reads; it
        # gates the terminal event on a PROVEN write and scores any other shape
        # zero. A writer returning, say, {"applied": True} has every message
        # logged as "Projection handler wrote zero rows" and its terminal
        # suppressed, including the messages that really did write.
        return {
            "rows_upserted": 0 if written is None else 1,
            "prod_promotion_gate_rows": [] if written is None else [written],
        }

    async def project_event(
        self, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> bool:
        """Standalone-runner entrypoint: project one message, report success."""
        await self._project_decision(data, meta)
        return True

    async def _project_decision(
        self, data: dict[str, Any], meta: MessageMeta
    ) -> dict[str, Any] | None:
        """Fold and persist one gate decision.

        Returns a compact description of the row the database accepted, or
        ``None`` when nothing was written. ``None`` is a real answer reported
        as such, never a truthy acknowledgement the runtime would read as a row.
        """
        event = ModelProdPromotionGateDecisionWire.model_validate(data)
        result = self._fold.handle(
            ModelProdPromotionGateProjectionRequest(
                event=event,
                fallback_correlation_id=meta.fallback_id,
                source_topic=meta.topic,
            )
        )
        row = result.row

        written = await self.db.execute(
            _UPSERT,
            row.correlation_id,
            row.outcome,
            row.allowed,
            row.reason,
            row.grant_id,
            row.requested_image_digest,
            row.resolved_image_digest,
            row.rollback_target,
            row.runtime_lane,
            row.promotion_batch_id,
            row.evaluated_at,
            row.source_topic,
            datetime.now(UTC),
        )
        if not written:
            return None

        accepted = dict(written[0])
        await self._publish_snapshot_if_available(accepted, meta)
        return {
            "correlation_id": str(accepted["correlation_id"]),
            "outcome": str(accepted["outcome"]),
            "allowed": bool(accepted["allowed"]),
            "grant_id": _or_none(accepted.get("grant_id")),
            "requested_image_digest": _or_none(accepted.get("requested_image_digest")),
            "evaluated_at": _isoformat(accepted.get("evaluated_at")),
        }

    async def _publish_snapshot_if_available(
        self, row: dict[str, Any], meta: MessageMeta
    ) -> None:
        """Best-effort snapshot publish (OMN-15800 Seam A).

        A no-op unless this node declares a ``bus_backed`` exposure. The row is
        what makes the refusal readable from the status page the operator
        already has open, rather than only from a table nobody has a client
        for -- which is the difference between a durable refusal and a useful
        one.
        """
        if self._snapshot_exposure is None:
            return
        await self.publish_snapshot_delta(
            self._snapshot_exposure,
            op="upsert",
            row=_jsonable(row),
            source_event_id=str(row.get("correlation_id") or meta.fallback_id),
            source_topic=meta.topic,
            source_partition=meta.partition,
            source_offset=meta.offset,
        )


def _jsonable(row: dict[str, Any]) -> dict[str, Any]:
    """Render a returned row for the snapshot wire.

    Reduced here rather than at the transport, where a value the encoder
    cannot handle would take the publish down with it.
    """
    return {
        key: (value.isoformat() if isinstance(value, datetime) else _or_str(value))
        for key, value in row.items()
    }


def _or_str(value: Any) -> Any:
    """Pass through JSON-native values; stringify anything else (UUIDs)."""
    if value is None or isinstance(value, bool | int | float | str):
        return value
    return str(value)


def _or_none(value: Any) -> str | None:
    """Stringify a present value, preserving a real absence as ``None``."""
    return None if value is None else str(value)


def _isoformat(value: Any) -> str | None:
    """Render a returned timestamp for the terminal event's payload."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


__all__ = ["ProdPromotionGateProjectionWriter"]
