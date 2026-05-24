# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
# onex-allow-internal-ip: lab GPU server fallback defaults for stability-test lane (read from env at runtime)
"""First real e2e projection test: delegation golden chain with real Postgres.

Chain: onex.evt.omniclaude.task-delegated.v1 -> delegation_events table

Guarded by OMN_ALLOW_LIVE_INTEGRATION_TESTS=true. Without the env var, all
tests in this module skip automatically. This test connects to the
stability-test lane Postgres on the lab GPU server (omnidash_analytics db).

Template for all future golden chain tests: OMN-11765.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from datetime import UTC, datetime
from typing import Any

import asyncpg
import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.kafka,
    pytest.mark.skipif(
        os.environ.get("OMN_ALLOW_LIVE_INTEGRATION_TESTS", "").lower() != "true",
        reason="Requires OMN_ALLOW_LIVE_INTEGRATION_TESTS=true",
    ),
]

# ---------------------------------------------------------------------------
# Stability-test lane connection constants (read from env, fallback to lab defaults)
# ---------------------------------------------------------------------------

_PG_HOST = os.environ.get(
    "INTEGRATION_POSTGRES_HOST",
    "192.168.86.201",  # onex-allow-internal-ip OMN-11765 reason="stability-test lane lab GPU server; read from env at runtime"
)
_PG_PORT = int(os.environ.get("INTEGRATION_POSTGRES_PORT", "15436"))
_PG_USER = os.environ.get("INTEGRATION_POSTGRES_USER", "postgres")
_PG_PASSWORD = os.environ.get(
    "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
)
_PG_DB = os.environ.get("INTEGRATION_POSTGRES_DB", "omnidash_analytics")

_KAFKA_BOOTSTRAP = os.environ.get(
    "KAFKA_BOOTSTRAP_SERVERS",
    "192.168.86.201:19092",  # onex-allow-internal-ip OMN-11765 reason="stability-test lane lab Redpanda; read from env at runtime"
)

_TOPIC_DELEGATED = "onex.evt.omniclaude.task-delegated.v1"
_TERMINAL_TOPIC = "onex.evt.omnimarket.projection-delegation-applied.v1"

_POLL_INTERVAL = 0.5
_POLL_TIMEOUT = 30.0


# ---------------------------------------------------------------------------
# Module-level fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def pg_conn() -> asyncpg.Connection:  # type: ignore[type-arg]
    """Direct asyncpg connection to stability-test omnidash_analytics."""
    if not _PG_PASSWORD:
        pytest.skip(
            "POSTGRES_PASSWORD not set — cannot connect to stability-test Postgres"
        )
    conn: asyncpg.Connection = await asyncpg.connect(  # type: ignore[type-arg]
        host=_PG_HOST,
        port=_PG_PORT,
        user=_PG_USER,
        password=_PG_PASSWORD,
        database=_PG_DB,
    )
    try:
        yield conn
    finally:
        await conn.close()


async def _wait_for_row(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    correlation_id: str,
    *,
    timeout: float = _POLL_TIMEOUT,
) -> dict[str, Any]:
    """Poll delegation_events until a row with correlation_id appears."""
    deadline = time.monotonic() + timeout
    while True:
        row = await conn.fetchrow(
            "SELECT * FROM delegation_events WHERE correlation_id = $1",
            correlation_id,
        )
        if row is not None:
            return dict(row)
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"No delegation_events row for correlation_id={correlation_id!r} "
                f"within {timeout}s"
            )
        await asyncio.sleep(_POLL_INTERVAL)


async def _row_count_for_correlation(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    correlation_id: str,
) -> int:
    result = await conn.fetchval(
        "SELECT COUNT(*) FROM delegation_events WHERE correlation_id = $1",
        correlation_id,
    )
    return int(result)


def _build_delegation_event(correlation_id: str) -> dict[str, Any]:
    return {
        "correlation_id": correlation_id,
        "task_type": "e2e_golden_chain_test",
        "delegated_to": "claude-haiku-4-5",
        "delegated_by": "omnimarket_e2e_test",
        "quality_gate_passed": True,
        "timestamp": datetime.now(UTC).isoformat(),
        "cost_usd": 0.0,
        "cost_savings_usd": 0.001,
        "pricing_manifest_version": 1,
    }


async def _publish_to_kafka(topic: str, payload: dict[str, Any]) -> tuple[int, int]:
    """Publish JSON payload to Kafka; return (partition, offset) metadata."""
    from aiokafka import AIOKafkaProducer
    from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope

    envelope = ModelEventEnvelope[dict[str, Any]](
        payload=payload,
        correlation_id=uuid.UUID(str(payload["correlation_id"])),
        source_tool="omnimarket.omn-11765-e2e",
        event_type="omniclaude.task-delegated",
    )

    producer = AIOKafkaProducer(
        bootstrap_servers=_KAFKA_BOOTSTRAP,
        value_serializer=lambda v: json.dumps(v).encode("utf-8"),
    )
    await producer.start()
    try:
        record_metadata = await producer.send_and_wait(
            topic, envelope.model_dump(mode="json")
        )
        return record_metadata.partition, record_metadata.offset
    finally:
        await producer.stop()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestDelegationGoldenChainE2E:
    """Full reducer chain: Kafka publish -> consumer -> delegation_events row."""

    async def test_event_published_and_row_written(
        self,
        pg_conn: asyncpg.Connection,  # type: ignore[type-arg]
    ) -> None:
        """Publish one task-delegated event; assert row appears in delegation_events."""
        correlation_id = str(uuid.uuid4())
        payload = _build_delegation_event(correlation_id)

        partition, offset = await _publish_to_kafka(_TOPIC_DELEGATED, payload)

        row = await _wait_for_row(pg_conn, correlation_id)

        assert row["correlation_id"] == correlation_id
        assert row["task_type"] == "e2e_golden_chain_test"
        assert row["delegated_to"] == "claude-haiku-4-5"
        assert row["quality_gate_passed"] is True

        evidence = {
            "topic": _TOPIC_DELEGATED,
            "partition": partition,
            "offset": offset,
            "correlation_id": correlation_id,
            "row_found": True,
        }
        assert evidence["row_found"] is True, f"Evidence chain: {evidence}"

    async def test_replay_idempotency(
        self,
        pg_conn: asyncpg.Connection,  # type: ignore[type-arg]
    ) -> None:
        """Replaying the same event must not create a duplicate row (ON CONFLICT DO NOTHING)."""
        correlation_id = str(uuid.uuid4())
        payload = _build_delegation_event(correlation_id)

        await _publish_to_kafka(_TOPIC_DELEGATED, payload)
        await _wait_for_row(pg_conn, correlation_id)

        count_after_first = await _row_count_for_correlation(pg_conn, correlation_id)
        assert count_after_first == 1

        await _publish_to_kafka(_TOPIC_DELEGATED, payload)
        await asyncio.sleep(5.0)

        count_after_replay = await _row_count_for_correlation(pg_conn, correlation_id)
        assert count_after_replay == 1, (
            f"Idempotency violated: expected 1 row, got {count_after_replay} "
            f"for correlation_id={correlation_id!r}"
        )

    async def test_required_fields_populated(
        self,
        pg_conn: asyncpg.Connection,  # type: ignore[type-arg]
    ) -> None:
        """All contract-declared columns receive correct values."""
        correlation_id = str(uuid.uuid4())
        payload = _build_delegation_event(correlation_id)

        await _publish_to_kafka(_TOPIC_DELEGATED, payload)
        row = await _wait_for_row(pg_conn, correlation_id)

        assert row["correlation_id"] == correlation_id
        assert row["task_type"] is not None
        assert row["delegated_to"] is not None
        assert row["quality_gate_passed"] is not None
        assert row["pricing_manifest_version"] == 1
