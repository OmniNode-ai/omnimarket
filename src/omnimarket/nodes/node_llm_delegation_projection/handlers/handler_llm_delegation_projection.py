# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""HandlerLlmDelegationProjection — reducer for daily LLM delegation aggregates.

Consumes onex.evt.omnimarket.delegation-call-completed.v1 events and
materializes daily aggregate rows into llm_delegation_daily_projection.

Idempotency: idempotency_key = correlation_id:causation_id:terminal_event_id.
Duplicate events (replay) are detected via the unique index on idempotency_key;
the UPSERT on (date, task_type, model_id, model_tier) accumulates new calls only
when idempotency_key is novel.

Ordering authority: Kafka topic/partition/offset. Timestamps are metadata only.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from omnimarket.events.topics import DELEGATION_CALL_COMPLETED_TOPIC_V1
from omnimarket.models.delegation.llm_cost_routing.model_llm_delegation_completed_event import (
    ModelLlmDelegationCompletedEvent,
)
from omnimarket.nodes.node_llm_delegation_projection.models.model_delegation_projection import (
    REDUCER_VERSION,
    EnumFreshnessState,
    ModelDelegationDailyAggregate,
    ModelDelegationProjectionResult,
)
from omnimarket.projection.protocol_database import DatabaseAdapter

TABLE = "llm_delegation_daily_projection"
CONFLICT_KEY = "projection_date, task_type, model_id, model_tier"
IDEMPOTENCY_TABLE_KEY = "idempotency_key"


def _build_idempotency_key(
    event: ModelLlmDelegationCompletedEvent, terminal_event_id: str
) -> str:
    return f"{event.correlation_id}:{event.causation_id}:{terminal_event_id}"


def _projection_cursor(topic: str, partition: int, offset: int) -> str:
    return f"{topic}:{partition}:{offset}"


def _projection_date(event: ModelLlmDelegationCompletedEvent) -> str:
    return event.created_at.astimezone(UTC).date().isoformat()


def _avg(total: Decimal | int, count: int) -> Decimal:
    if count == 0:
        return Decimal("0")
    return Decimal(str(total)) / Decimal(str(count))


class HandlerLlmDelegationProjection:
    """Reduce delegation-call-completed events into daily aggregate rows."""

    def handle(self, input_data: dict[str, Any]) -> dict[str, Any]:
        """RuntimeLocal handler shim. Expects _db, _topic, _partition, _offset keys."""
        payload = dict(input_data)
        db_raw = payload.pop("_db", None)
        if not isinstance(db_raw, DatabaseAdapter):
            raise TypeError("handle() requires a DatabaseAdapter in input_data['_db']")

        topic = str(payload.pop("_topic", DELEGATION_CALL_COMPLETED_TOPIC_V1))
        partition = int(payload.pop("_partition", 0))
        offset = int(payload.pop("_offset", 0))
        terminal_event_id = str(
            payload.pop("_terminal_event_id", payload.get("request_id", ""))
        )

        event = ModelLlmDelegationCompletedEvent(**payload)
        result = self.project(
            event,
            db_raw,
            topic=topic,
            partition=partition,
            offset=offset,
            terminal_event_id=terminal_event_id,
        )
        return result.model_dump(mode="json")

    def project(
        self,
        event: ModelLlmDelegationCompletedEvent,
        db: DatabaseAdapter,
        *,
        topic: str,
        partition: int,
        offset: int,
        terminal_event_id: str,
        freshness_state: EnumFreshnessState = EnumFreshnessState.FRESH,
    ) -> ModelDelegationProjectionResult:
        """Materialize one completed event into the daily aggregate table.

        Idempotency contract:
        - Build idempotency_key from correlation_id:causation_id:terminal_event_id.
        - Check whether that key already exists in the table.
        - If it does: skip aggregate accumulation (no double-counting).
        - If it doesn't: UPSERT the aggregate row with accumulated deltas.
        """
        idempotency_key = _build_idempotency_key(event, terminal_event_id)
        cursor = _projection_cursor(topic, partition, offset)
        date_str = _projection_date(event)

        # Idempotency check — replay safety
        existing = db.query(TABLE, {IDEMPOTENCY_TABLE_KEY: idempotency_key})
        if existing:
            return ModelDelegationProjectionResult(
                rows_upserted=0,
                idempotency_key=idempotency_key,
                projection_cursor=cursor,
                skipped_duplicate=True,
            )

        # Read existing aggregate row for this (date, task_type, model_id, model_tier)
        current_rows = db.query(
            TABLE,
            {
                "projection_date": date_str,
                "task_type": event.task_type,
                "model_id": event.model_id,
                "model_tier": event.model_tier,
            },
        )
        current = current_rows[0] if current_rows else None

        # Accumulate deltas
        prev_calls = int(current["total_calls"]) if current else 0
        new_total_calls = prev_calls + 1
        new_successful = (int(current["successful_calls"]) if current else 0) + (
            1 if event.success else 0
        )
        new_escalated = (int(current["escalated_calls"]) if current else 0) + (
            1 if event.escalated_to is not None else 0
        )
        new_tokens_in = (
            int(current["total_tokens_in"]) if current else 0
        ) + event.tokens_in
        new_tokens_out = (
            int(current["total_tokens_out"]) if current else 0
        ) + event.tokens_out
        new_latency_ms = (
            int(current["total_latency_ms"]) if current else 0
        ) + event.latency_ms
        new_actual_cost = (
            Decimal(str(current["total_actual_cost_usd"])) if current else Decimal("0")
        ) + event.actual_cost_usd
        new_opus_cost = (
            Decimal(str(current["total_opus_equivalent_usd"]))
            if current
            else Decimal("0")
        ) + event.opus_equivalent_cost_usd
        new_savings = (
            Decimal(str(current["total_savings_usd"])) if current else Decimal("0")
        ) + event.savings_usd

        # Incremental average quality score
        prev_avg_quality = (
            float(current["avg_quality_score"])
            if current and current.get("avg_quality_score") is not None
            else None
        )
        if event.quality_score is not None:
            if prev_avg_quality is not None:
                new_avg_quality: float | None = (
                    prev_avg_quality * prev_calls + event.quality_score
                ) / new_total_calls
            else:
                new_avg_quality = event.quality_score
        else:
            new_avg_quality = prev_avg_quality

        aggregate = ModelDelegationDailyAggregate(
            projection_date=date_str,
            task_type=event.task_type,
            model_id=event.model_id,
            model_tier=event.model_tier,
            total_calls=new_total_calls,
            successful_calls=new_successful,
            escalated_calls=new_escalated,
            total_tokens_in=new_tokens_in,
            total_tokens_out=new_tokens_out,
            total_latency_ms=new_latency_ms,
            avg_latency_ms=_avg(new_latency_ms, new_total_calls),
            total_actual_cost_usd=new_actual_cost,
            total_opus_equivalent_usd=new_opus_cost,
            total_savings_usd=new_savings,
            avg_quality_score=new_avg_quality,
            projection_cursor=cursor,
            source_event_id=event.request_id,
            source_topic=topic,
            source_partition=partition,
            source_offset=offset,
            freshness_state=freshness_state,
            reducer_version=REDUCER_VERSION,
            idempotency_key=idempotency_key,
        )

        row: dict[str, object] = {
            "projection_date": aggregate.projection_date,
            "task_type": aggregate.task_type,
            "model_id": aggregate.model_id,
            "model_tier": aggregate.model_tier,
            "total_calls": aggregate.total_calls,
            "successful_calls": aggregate.successful_calls,
            "escalated_calls": aggregate.escalated_calls,
            "total_tokens_in": aggregate.total_tokens_in,
            "total_tokens_out": aggregate.total_tokens_out,
            "total_latency_ms": aggregate.total_latency_ms,
            "avg_latency_ms": str(aggregate.avg_latency_ms),
            "total_actual_cost_usd": str(aggregate.total_actual_cost_usd),
            "total_opus_equivalent_usd": str(aggregate.total_opus_equivalent_usd),
            "total_savings_usd": str(aggregate.total_savings_usd),
            "avg_quality_score": aggregate.avg_quality_score,
            "projection_cursor": aggregate.projection_cursor,
            "source_event_id": aggregate.source_event_id,
            "source_topic": aggregate.source_topic,
            "source_partition": aggregate.source_partition,
            "source_offset": aggregate.source_offset,
            "freshness_state": aggregate.freshness_state.value,
            "reducer_version": aggregate.reducer_version,
            "idempotency_key": aggregate.idempotency_key,
            "observed_at": datetime.now(tz=UTC).isoformat(),
            "updated_at": datetime.now(tz=UTC).isoformat(),
        }

        ok = db.upsert(TABLE, CONFLICT_KEY, row)
        return ModelDelegationProjectionResult(
            rows_upserted=1 if ok else 0,
            idempotency_key=idempotency_key,
            projection_cursor=cursor,
            skipped_duplicate=False,
        )


__all__: list[str] = [
    "CONFLICT_KEY",
    "TABLE",
    "HandlerLlmDelegationProjection",
]
