# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Kafka consumer for node_projection_llm_cost.

Subscribes to ``onex.evt.omniintelligence.llm-call-completed.v1`` and
materializes each event into the ``llm_call_metrics`` table using
``ON CONFLICT DO NOTHING`` keyed on ``input_hash`` (migration 071).

Environment variables (resolved at startup, no hardcoded strings):
    KAFKA_BOOTSTRAP_SERVERS  Redpanda bootstrap (default: localhost:9092)
    KAFKA_CONSUMER_GROUP     Consumer group override
    OMNIDASH_ANALYTICS_DB_URL  asyncpg DSN (required)
    POSTGRES_PASSWORD        Only needed if constructing DSN manually

Usage:
    uv run python -m omnimarket.nodes.node_projection_llm_cost.consumer \\
        --bootstrap-servers 192.168.86.201:19092
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import uuid
from typing import Any

from omnimarket.nodes.node_projection_llm_cost.handlers.handler_projection_llm_cost import (
    TOPIC_LLM_CALL_COMPLETED,
)
from omnimarket.projection.envelope import unwrap_envelope
from omnimarket.projection.runner import safe_parse_date

_log = logging.getLogger(__name__)

SUBSCRIBE_TOPIC = TOPIC_LLM_CALL_COMPLETED
CONSUMER_GROUP = "local.omnimarket.projection-llm-cost.consume.v1"
TABLE = "llm_call_metrics"

REQUIRED_FIELDS = frozenset(
    {
        "model_name",
        "model_id",
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
    }
)


def _safe_int(value: Any, default: int = 0) -> int:
    if value is None:
        return default
    try:
        return int(float(value))
    except (ValueError, TypeError):
        return default


def _safe_float(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except (ValueError, TypeError):
        return default


def _validate_event(data: dict[str, Any]) -> bool:
    """Return True if event has enough fields to be worth projecting."""
    has_model = bool(data.get("model_name") or data.get("model_id"))
    has_tokens = any(
        data.get(f) is not None
        for f in ("prompt_tokens", "completion_tokens", "total_tokens")
    )
    return has_model and has_tokens


def _build_row(data: dict[str, Any]) -> dict[str, Any]:
    """Map inbound event fields to llm_call_metrics columns."""
    model_id = str(data.get("model_id") or data.get("model_name") or "unknown")
    session_id = data.get("session_id") or data.get("sessionId")
    correlation_id_raw = (
        data.get("correlation_id") or data.get("_correlation_id") or data.get("call_id")
    )

    # Coerce correlation_id to UUID or None — column type is UUID
    correlation_id: str | None = None
    if correlation_id_raw:
        try:
            correlation_id = str(uuid.UUID(str(correlation_id_raw)))
        except ValueError:
            _log.debug(
                "correlation_id %r is not a valid UUID, storing None",
                correlation_id_raw,
            )

    prompt_tokens = _safe_int(
        data.get("prompt_tokens")
        or data.get("input_tokens")
        or data.get("promptTokens")
    )
    completion_tokens = _safe_int(
        data.get("completion_tokens")
        or data.get("output_tokens")
        or data.get("completionTokens")
    )
    total_tokens = _safe_int(
        data.get("total_tokens")
        or data.get("totalTokens")
        or (prompt_tokens + completion_tokens)
    )
    estimated_cost_usd = _safe_float(
        data.get("estimated_cost_usd")
        or data.get("estimatedCostUsd")
        or data.get("cost_usd")
    )
    latency_ms_raw = data.get("latency_ms") or data.get("latencyMs")
    latency_ms: float | None = (
        _safe_float(latency_ms_raw) if latency_ms_raw is not None else None
    )

    usage_source_raw = str(
        data.get("usage_source") or data.get("usageSource") or "MISSING"
    ).upper()
    valid_usage_sources = {"API", "ESTIMATED", "MISSING"}
    usage_source = (
        usage_source_raw if usage_source_raw in valid_usage_sources else "MISSING"
    )
    usage_is_estimated = usage_source == "ESTIMATED"

    # input_hash is the dedup key (migration 071 unique index)
    input_hash = (
        data.get("input_hash")
        or data.get("idempotency_key")
        or data.get("call_id")
        or None
    )
    if input_hash is not None:
        input_hash = str(input_hash)[:71]

    # Parse created_at from event timestamp fields
    timestamp_raw = (
        data.get("timestamp")
        or data.get("timestamp_iso")
        or data.get("created_at")
        or data.get("createdAt")
    )
    created_at = safe_parse_date(timestamp_raw)

    return {
        "correlation_id": correlation_id,
        "session_id": str(session_id)[:255] if session_id else None,
        "model_id": model_id[:255],
        "prompt_tokens": prompt_tokens or None,
        "completion_tokens": completion_tokens or None,
        "total_tokens": total_tokens or None,
        "estimated_cost_usd": estimated_cost_usd if estimated_cost_usd else None,
        "latency_ms": latency_ms,
        "usage_source": usage_source,
        "usage_is_estimated": usage_is_estimated,
        "input_hash": input_hash,
        "created_at": created_at,
    }


async def _insert_row(db: Any, row: dict[str, Any]) -> bool:
    """Insert a row into llm_call_metrics with ON CONFLICT DO NOTHING."""
    await db.execute(
        f"""
        INSERT INTO {TABLE} (
            correlation_id, session_id, model_id,
            prompt_tokens, completion_tokens, total_tokens,
            estimated_cost_usd, latency_ms,
            usage_source, usage_is_estimated,
            input_hash, created_at
        ) VALUES (
            $1, $2, $3,
            $4, $5, $6,
            $7, $8,
            $9::usage_source_type, $10,
            $11, $12
        )
        ON CONFLICT DO NOTHING
        """,
        row["correlation_id"],
        row["session_id"],
        row["model_id"],
        row["prompt_tokens"],
        row["completion_tokens"],
        row["total_tokens"],
        row["estimated_cost_usd"],
        row["latency_ms"],
        row["usage_source"],
        row["usage_is_estimated"],
        row["input_hash"],
        row["created_at"],
    )
    return True


async def _run_consumer(broker: str, group_id: str, db_dsn: str) -> None:
    try:
        from aiokafka import AIOKafkaConsumer
    except ImportError:
        _log.error("aiokafka is not installed; run: uv add aiokafka")
        raise

    from omnimarket.adapters.asyncpg_adapter import AsyncpgAdapter

    db = AsyncpgAdapter(dsn=db_dsn)
    await db.connect()
    _log.info("DB connected")

    consumer = AIOKafkaConsumer(
        SUBSCRIBE_TOPIC,
        bootstrap_servers=broker,
        group_id=group_id,
        auto_offset_reset="earliest",
        enable_auto_commit=True,
    )
    await consumer.start()
    _log.info(
        "projection-llm-cost consumer started — broker=%s group=%s topic=%s",
        broker,
        group_id,
        SUBSCRIBE_TOPIC,
    )

    stop_event = asyncio.Event()

    def _signal_handler(sig: int, _: Any) -> None:
        _log.info("received signal %s, shutting down", sig)
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, _signal_handler)

    projected = 0
    skipped = 0
    errors = 0

    try:
        async for msg in consumer:
            if stop_event.is_set():
                break

            raw_value = msg.value
            if raw_value is None:
                continue

            try:
                data = unwrap_envelope(raw_value)
            except Exception as exc:
                _log.error("failed to parse message offset=%d: %s", msg.offset, exc)
                errors += 1
                continue

            if data is None:
                _log.warning("non-JSON message at offset=%d, skipping", msg.offset)
                skipped += 1
                continue

            if not _validate_event(data):
                _log.warning(
                    "malformed event at offset=%d missing required fields, skipping — keys=%s",
                    msg.offset,
                    list(data.keys()),
                )
                skipped += 1
                continue

            try:
                row = _build_row(data)
                await _insert_row(db, row)
                projected += 1
                _log.debug(
                    "projected model_id=%s input_hash=%s offset=%d",
                    row["model_id"],
                    row["input_hash"],
                    msg.offset,
                )
            except Exception as exc:
                _log.error(
                    "projection failed at offset=%d model=%s: %s",
                    msg.offset,
                    data.get("model_id") or data.get("model_name"),
                    exc,
                    exc_info=True,
                )
                errors += 1

    finally:
        await consumer.stop()
        await db.close()
        _log.info(
            "consumer stopped — projected=%d skipped=%d errors=%d",
            projected,
            skipped,
            errors,
        )


def _build_dsn() -> str:
    dsn = os.environ.get("OMNIDASH_ANALYTICS_DB_URL")
    if dsn:
        return dsn
    host = os.environ.get("POSTGRES_HOST", "192.168.86.201")
    port = os.environ.get("POSTGRES_PORT", "5436")
    user = os.environ.get("POSTGRES_USER", "postgres")
    password = os.environ["POSTGRES_PASSWORD"]
    db = os.environ.get("POSTGRES_DB", "omnibase_infra")
    return f"postgresql://{user}:{password}@{host}:{port}/{db}"


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser(
        description="Kafka→Postgres projection consumer for LLM cost events"
    )
    parser.add_argument(
        "--bootstrap-servers",
        default=os.environ.get("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092"),
        help="Kafka bootstrap servers (env: KAFKA_BOOTSTRAP_SERVERS)",
    )
    parser.add_argument(
        "--group-id",
        default=os.environ.get("KAFKA_CONSUMER_GROUP", CONSUMER_GROUP),
        help="Consumer group ID",
    )
    args = parser.parse_args()

    dsn = _build_dsn()
    asyncio.run(_run_consumer(args.bootstrap_servers, args.group_id, dsn))


if __name__ == "__main__":
    main()
