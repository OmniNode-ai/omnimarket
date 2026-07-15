"""Baselines projection: Kafka -> 4 tables transactionally.

OMN-14513: this runner previously parsed the incoming payload via raw dict
``.get()`` chains against field names (``pattern_id``/``token_delta``/
``date``/``avg_cost_savings``/``action``/``count``) invented independently of
the real producer contract
(``omnibase_infra.services.observability.baselines.models.model_baselines_snapshot_event.ModelBaselinesSnapshotEvent``),
which shares almost no field names with them. Every real event silently
degraded to all-default rows keyed on a blank ``pattern_id`` that the
producer never sends. Fixed to validate the canonical producer model
directly (market is the top layer: compat < core < spi < infra < market, so
this import is legal) and to write the columns declared by
``migrations/0002_realign_child_tables_to_producer_schema.sql``.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import yaml
from omnibase_infra.services.observability.baselines.models.model_baselines_snapshot_event import (
    ModelBaselinesSnapshotEvent,
)

from omnimarket.projection.dlq import dlq_topics_from_contract
from omnimarket.projection.runner import BaseProjectionRunner, MessageMeta, PublishFn

logger = logging.getLogger(__name__)

HANDLER_ID_PROJECTION_BASELINES = "node_projection_baselines"

KNOWN_PROJECTION_TABLES: frozenset[str] = frozenset(
    {
        "delegation_events",
        "delegation_shadow_comparisons",
        "llm_cost_aggregates",
        "node_service_registry",
        "baselines_snapshots",
        "baselines_comparisons",
        "baselines_trend",
        "baselines_breakdown",
        "savings_estimates",
        "session_outcomes",
        "injection_effectiveness",
    }
)

MAX_BATCH_ROWS = 4000


def _strip_transport_keys(data: dict[str, Any]) -> dict[str, Any]:
    """Drop runtime/transport-injected ``_``-prefixed keys from a decoded payload.

    The canonical producer model is ``extra="forbid"``; ``unwrap_envelope``
    attaches ``_envelope``/``_event_type``/``_correlation_id``, which would
    otherwise raise on every message.
    """
    return {k: v for k, v in data.items() if not str(k).startswith("_")}


class BaselinesProjectionRunner(BaseProjectionRunner):
    """Projects baselines-computed events into 4 tables transactionally.

    Tables: baselines_snapshots, baselines_comparisons, baselines_trend, baselines_breakdown
    Uses DELETE+INSERT for child tables within a transaction.
    Matches omnidash projectBaselinesSnapshot() exactly.
    """

    def __init__(
        self,
        contract_path: Path | None = None,
        *,
        publish_fn: PublishFn | None = None,
    ) -> None:
        super().__init__(publish_fn=publish_fn)
        _path = contract_path or Path(__file__).parent.parent / "contract.yaml"
        with open(_path) as f:
            self._contract: dict[str, Any] = yaml.safe_load(f)

        _tables = self._contract.get("db_io", {}).get("db_tables", [])
        _by_role = {t["role"]: t["name"] for t in _tables}

        for role, name in _by_role.items():
            if name not in KNOWN_PROJECTION_TABLES:
                raise ValueError(
                    f"Unknown table role {role!r} maps to {name!r} which is not in KNOWN_PROJECTION_TABLES"
                )

        for required_role in ("snapshots", "comparisons", "trend", "breakdown"):
            if required_role not in _by_role:
                raise ValueError(
                    f"Contract missing required table role {required_role!r}"
                )

        self._table_snapshots: str = _by_role["snapshots"]
        self._table_comparisons: str = _by_role["comparisons"]
        self._table_trend: str = _by_role["trend"]
        self._table_breakdown: str = _by_role["breakdown"]
        # OMN-14513 / OMN-13548 (D-03): a ValidationError raised inside
        # project_event() while parsing the real producer's
        # ModelBaselinesSnapshotEvent now emits a DURABLE failure signal on
        # the contract-declared DLQ topic (via the base class's own POISON
        # safety net in _handle_message) instead of being logged and dropped
        # silently.
        self._dlq_topics: list[str] = dlq_topics_from_contract(self._contract)

    @property
    def poison_dlq_topics(self) -> list[str]:
        """OMN-13634: base-class safety net routes escaped POISON errors here."""
        return self._dlq_topics

    async def publish_dlq(self, topic: str, value: bytes) -> None:
        """OMN-13634: supply the runtime-owned publisher to the base-class DLQ path."""
        publish = await self.get_publish_fn()
        if publish is None:
            logger.error(
                "node_projection_baselines: no publisher for POISON DLQ topic %s",
                topic,
            )
            return
        await publish(topic, value)

    @property
    def subscribe_topics(self) -> list[str]:
        return list(self._contract.get("event_bus", {}).get("subscribe_topics", []))

    def handle(self, input_data: dict[str, Any]) -> dict[str, Any]:
        """RuntimeLocal handler protocol shim.

        Delegates to project_event via asyncio.run().
        """
        topics = self.subscribe_topics
        topic = str(input_data.pop("_topic", topics[0] if topics else ""))
        meta = MessageMeta(
            partition=int(input_data.pop("_partition", 0)),
            offset=int(input_data.pop("_offset", 0)),
            fallback_id=str(input_data.pop("_fallback_id", "")),
        )
        ok = asyncio.run(self.project_event(topic, input_data, meta))
        return {"projected": ok}

    @property
    def topics(self) -> list[str]:
        return self.subscribe_topics

    async def project_event(
        self, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> bool:
        event = ModelBaselinesSnapshotEvent(**_strip_transport_keys(data))
        snapshot_id = str(event.snapshot_id)

        comparisons = event.comparisons[:MAX_BATCH_ROWS]
        if len(event.comparisons) > MAX_BATCH_ROWS:
            logger.warning(
                "baselines snapshot %s: %d comparison rows, capping at %d",
                snapshot_id,
                len(event.comparisons),
                MAX_BATCH_ROWS,
            )
        trend = event.trend[:MAX_BATCH_ROWS]
        if len(event.trend) > MAX_BATCH_ROWS:
            logger.warning(
                "baselines snapshot %s: %d trend rows, capping at %d",
                snapshot_id,
                len(event.trend),
                MAX_BATCH_ROWS,
            )
        breakdown = event.breakdown[:MAX_BATCH_ROWS]
        if len(event.breakdown) > MAX_BATCH_ROWS:
            logger.warning(
                "baselines snapshot %s: %d breakdown rows, capping at %d",
                snapshot_id,
                len(event.breakdown),
                MAX_BATCH_ROWS,
            )

        # Execute all in a single transaction
        queries: list[tuple[str, tuple[Any, ...]]] = []

        # 1. Upsert snapshot header
        queries.append(
            (
                f"""
            INSERT INTO {self._table_snapshots} (
              snapshot_id, contract_version, computed_at_utc, window_start_utc, window_end_utc
            ) VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (snapshot_id) DO UPDATE SET
              contract_version = EXCLUDED.contract_version,
              computed_at_utc = EXCLUDED.computed_at_utc,
              window_start_utc = EXCLUDED.window_start_utc,
              window_end_utc = EXCLUDED.window_end_utc,
              projected_at = NOW()
            """,
                (
                    snapshot_id,
                    event.contract_version,
                    event.computed_at_utc,
                    event.window_start_utc,
                    event.window_end_utc,
                ),
            )
        )

        # 2. Delete + re-insert comparisons
        queries.append(
            (
                f"DELETE FROM {self._table_comparisons} WHERE snapshot_id = $1",
                (snapshot_id,),
            )
        )
        for comp in comparisons:
            queries.append(
                (
                    f"""
                INSERT INTO {self._table_comparisons} (
                  id, snapshot_id, comparison_date, period_label,
                  treatment_sessions, treatment_success_rate, treatment_avg_latency_ms,
                  treatment_avg_cost_tokens, treatment_total_tokens,
                  control_sessions, control_success_rate, control_avg_latency_ms,
                  control_avg_cost_tokens, control_total_tokens,
                  roi_pct, latency_improvement_pct, cost_improvement_pct,
                  sample_size, computed_at, created_at, updated_at
                ) VALUES (
                  $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12,
                  $13, $14, $15, $16, $17, $18, $19, $20, $21
                )
                """,
                    (
                        str(comp.id),
                        snapshot_id,
                        comp.comparison_date,
                        comp.period_label,
                        comp.treatment_sessions,
                        comp.treatment_success_rate,
                        comp.treatment_avg_latency_ms,
                        comp.treatment_avg_cost_tokens,
                        comp.treatment_total_tokens,
                        comp.control_sessions,
                        comp.control_success_rate,
                        comp.control_avg_latency_ms,
                        comp.control_avg_cost_tokens,
                        comp.control_total_tokens,
                        comp.roi_pct,
                        comp.latency_improvement_pct,
                        comp.cost_improvement_pct,
                        comp.sample_size,
                        comp.computed_at,
                        comp.created_at,
                        comp.updated_at,
                    ),
                )
            )

        # 3. Delete + re-insert trend
        queries.append(
            (
                f"DELETE FROM {self._table_trend} WHERE snapshot_id = $1",
                (snapshot_id,),
            )
        )
        for tr in trend:
            queries.append(
                (
                    f"""
                INSERT INTO {self._table_trend} (
                  id, snapshot_id, trend_date, cohort, session_count,
                  success_rate, avg_latency_ms, avg_cost_tokens, roi_pct,
                  computed_at, created_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                """,
                    (
                        str(tr.id),
                        snapshot_id,
                        tr.trend_date,
                        tr.cohort,
                        tr.session_count,
                        tr.success_rate,
                        tr.avg_latency_ms,
                        tr.avg_cost_tokens,
                        tr.roi_pct,
                        tr.computed_at,
                        tr.created_at,
                    ),
                )
            )

        # 4. Delete + re-insert breakdown
        queries.append(
            (
                f"DELETE FROM {self._table_breakdown} WHERE snapshot_id = $1",
                (snapshot_id,),
            )
        )
        for bd in breakdown:
            queries.append(
                (
                    f"""
                INSERT INTO {self._table_breakdown} (
                  id, snapshot_id, pattern_id, pattern_label,
                  treatment_success_rate, control_success_rate, roi_pct,
                  sample_count, treatment_count, control_count, confidence,
                  computed_at, created_at, updated_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)
                """,
                    (
                        str(bd.id),
                        snapshot_id,
                        str(bd.pattern_id),
                        bd.pattern_label,
                        bd.treatment_success_rate,
                        bd.control_success_rate,
                        bd.roi_pct,
                        bd.sample_count,
                        bd.treatment_count,
                        bd.control_count,
                        bd.confidence,
                        bd.computed_at,
                        bd.created_at,
                        bd.updated_at,
                    ),
                )
            )

        await self.db.execute_in_transaction(queries)

        logger.info(
            "Projected baselines snapshot %s (%d comparisons, %d trend, %d breakdown)",
            snapshot_id,
            len(comparisons),
            len(trend),
            len(breakdown),
        )
        return True


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )
    runner = BaselinesProjectionRunner()
    asyncio.run(runner.run())
