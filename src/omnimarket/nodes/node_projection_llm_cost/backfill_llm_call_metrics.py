# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Backfill/replay entrypoint for node_projection_llm_cost.

OMN-13001: this is NOT the runtime writer. The deployed runtime writer is
handler_llm_cost.LlmCostProjectionRunner (single write authority for
``llm_call_metrics``). This module is the explicit, operator-invoked backfill
path: it replays the retained
``onex.evt.omniintelligence.llm-call-completed.v1`` topic FROM EARLIEST into
``llm_call_metrics`` so historical token-bearing events that predate the writer
fix are materialized.

It shares ``build_llm_call_metrics_row`` with the runtime writer, so the column
set and dedup key are identical. Inserts use ``ON CONFLICT (input_hash) DO
NOTHING`` — replay is idempotent and never duplicates rows, so re-running the
backfill (or running it concurrently with the live writer) is safe.

Environment variables (resolved at startup, no hardcoded strings):
    KAFKA_BOOTSTRAP_SERVERS    Redpanda bootstrap (required — no default)
    KAFKA_CONSUMER_GROUP       Consumer group override
    OMNIDASH_ANALYTICS_DB_URL  asyncpg DSN (required)
    POSTGRES_PASSWORD          Only needed if constructing DSN manually

Usage:
    uv run python -m omnimarket.nodes.node_projection_llm_cost.backfill_llm_call_metrics \\
        --bootstrap-servers "$KAFKA_BOOTSTRAP_SERVERS" \\
        --until-idle-seconds 15
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
from typing import Any

from omnimarket.nodes.node_projection_llm_cost.handlers.handler_llm_cost import (
    CONFLICT_KEY,
    TABLE,
)
from omnimarket.nodes.node_projection_llm_cost.handlers.handler_projection_llm_cost import (
    TOPIC_LLM_CALL_COMPLETED,
)
from omnimarket.nodes.node_projection_llm_cost.handlers.row_llm_call_metrics import (
    build_llm_call_metrics_row,
    event_has_projectable_fields,
)
from omnimarket.projection.envelope import unwrap_envelope

_log = logging.getLogger(__name__)

SUBSCRIBE_TOPIC = TOPIC_LLM_CALL_COMPLETED
DEFAULT_BACKFILL_GROUP = "local.omnimarket.projection-llm-cost.backfill.v1"


async def _insert_row(db: Any, row: dict[str, Any]) -> bool:
    """Insert a row into llm_call_metrics with ON CONFLICT (input_hash) DO NOTHING.

    Returns True once the insert statement has been issued (the ON CONFLICT
    clause makes the insert a no-op for an already-present input_hash).
    """
    await db.execute(
        f"""
        INSERT INTO {TABLE} (
            correlation_id, session_id, run_id, model_id,
            prompt_tokens, completion_tokens, total_tokens,
            estimated_cost_usd, latency_ms,
            usage_source, usage_is_estimated, usage_raw,
            input_hash, source,
            code_version, contract_version,
            created_at
        ) VALUES (
            $1, $2, $3, $4,
            $5, $6, $7,
            $8, $9,
            $10::usage_source_type, $11, $12::jsonb,
            $13, $14,
            $15, $16,
            $17
        )
        ON CONFLICT ({CONFLICT_KEY}) DO NOTHING
        """,
        row["correlation_id"],
        row["session_id"],
        row["run_id"],
        row["model_id"],
        row["prompt_tokens"],
        row["completion_tokens"],
        row["total_tokens"],
        row["estimated_cost_usd"],
        row["latency_ms"],
        row["usage_source"],
        row["usage_is_estimated"],
        row["usage_raw"],
        row["input_hash"],
        row["source"],
        row["code_version"],
        row["contract_version"],
        row["created_at"],
    )
    return True


async def _run_backfill(
    broker: str, group_id: str, db_dsn: str, until_idle_seconds: float
) -> None:
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
        "projection-llm-cost backfill started — broker=%s group=%s topic=%s "
        "(reading from earliest, idle-stop=%.1fs)",
        broker,
        group_id,
        SUBSCRIBE_TOPIC,
        until_idle_seconds,
    )

    stop_event = asyncio.Event()

    def _signal_handler(sig: int, _frame: Any) -> None:
        _log.info("received signal %s, shutting down", sig)
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, _signal_handler)

    projected = 0
    skipped = 0
    errors = 0

    try:
        while not stop_event.is_set():
            batch = await consumer.getmany(timeout_ms=int(until_idle_seconds * 1000))
            if not batch:
                _log.info(
                    "no new records for %.1fs — backfill complete", until_idle_seconds
                )
                break
            for _tp, messages in batch.items():
                for msg in messages:
                    raw_value = msg.value
                    if raw_value is None:
                        continue
                    try:
                        data = unwrap_envelope(raw_value)
                    except Exception as exc:
                        _log.error("failed to parse offset=%d: %s", msg.offset, exc)
                        errors += 1
                        continue
                    if data is None:
                        skipped += 1
                        continue
                    if not event_has_projectable_fields(data):
                        skipped += 1
                        continue
                    try:
                        await _insert_row(db, build_llm_call_metrics_row(data))
                        projected += 1
                    except Exception as exc:
                        _log.error(
                            "backfill insert failed at offset=%d: %s",
                            msg.offset,
                            exc,
                            exc_info=True,
                        )
                        errors += 1
    finally:
        await consumer.stop()
        await db.close()
        _log.info(
            "backfill stopped — projected=%d skipped=%d errors=%d",
            projected,
            skipped,
            errors,
        )


def _build_dsn() -> str:
    dsn = os.environ.get("OMNIDASH_ANALYTICS_DB_URL")
    if dsn:
        return dsn
    host = os.environ.get("POSTGRES_HOST", "")
    port = os.environ.get("POSTGRES_PORT", "5436")
    user = os.environ.get("POSTGRES_USER", "postgres")
    password = os.environ["POSTGRES_PASSWORD"]
    database = os.environ.get("POSTGRES_DB", "omnibase_infra")
    return f"postgresql://{user}:{password}@{host}:{port}/{database}"


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser(
        description="Idempotent backfill: replay llm-call-completed from earliest into llm_call_metrics"
    )
    parser.add_argument(
        "--bootstrap-servers",
        default=os.environ.get("KAFKA_BOOTSTRAP_SERVERS", ""),
        help="Kafka bootstrap servers (env: KAFKA_BOOTSTRAP_SERVERS)",
    )
    parser.add_argument(
        "--group-id",
        default=os.environ.get("KAFKA_CONSUMER_GROUP", DEFAULT_BACKFILL_GROUP),
        help="Consumer group ID (use a fresh group to replay from earliest)",
    )
    parser.add_argument(
        "--until-idle-seconds",
        type=float,
        default=15.0,
        help="Stop after this many seconds with no new records (drains then exits)",
    )
    args = parser.parse_args()

    if not args.bootstrap_servers:
        parser.error("--bootstrap-servers (or KAFKA_BOOTSTRAP_SERVERS) is required")

    dsn = _build_dsn()
    asyncio.run(
        _run_backfill(
            args.bootstrap_servers, args.group_id, dsn, args.until_idle_seconds
        )
    )


if __name__ == "__main__":
    main()
