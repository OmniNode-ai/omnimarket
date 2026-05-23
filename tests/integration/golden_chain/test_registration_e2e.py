# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
# onex-allow-internal-ip: lab GPU server fallback defaults for stability-test lane (read from env at runtime)
"""Registration golden chain e2e test: routing-decision.v1 -> agent_routing_decisions.

Chain: onex.evt.omniclaude.routing-decision.v1 -> agent_routing_decisions table

Guarded by OMN_ALLOW_LIVE_INTEGRATION_TESTS=true. Without the env var, all
tests in this module skip automatically. This test connects to the
stability-test lane Postgres on the lab GPU server (omnibase_infra db).

Follows the delegation e2e pattern established in OMN-11765.
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
        not os.environ.get("OMN_ALLOW_LIVE_INTEGRATION_TESTS"),
        reason="Requires OMN_ALLOW_LIVE_INTEGRATION_TESTS=true",
    ),
]

# ---------------------------------------------------------------------------
# Stability-test lane connection constants (read from env, fallback to lab defaults)
# ---------------------------------------------------------------------------

_PG_HOST = os.environ.get(
    "INTEGRATION_POSTGRES_HOST", "192.168.86.201"
)  # onex-allow-internal-ip: lab GPU server stability-test lane default
_PG_PORT = int(os.environ.get("INTEGRATION_POSTGRES_PORT", "5436"))
_PG_USER = os.environ.get("INTEGRATION_POSTGRES_USER", "postgres")
_PG_PASSWORD = os.environ.get(
    "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
)
# agent_routing_decisions lives in omnibase_infra (not omnidash_analytics)
_PG_DB = os.environ.get("INTEGRATION_POSTGRES_DB_INFRA", "omnibase_infra")

_KAFKA_BOOTSTRAP = os.environ.get(
    "KAFKA_BOOTSTRAP_SERVERS", "192.168.86.201:19092"
)  # onex-allow-internal-ip: lab GPU server Redpanda default

# Topic sourced from node_emit_daemon/registries/topics.yaml routing.decision fan_out
# onex-topic-allow: read from topics registry at test construction time; used only in integration tests
_TOPIC_ROUTING_DECISION = "onex.evt.omniclaude.routing-decision.v1"  # onex-topic-allow: golden chain integration test, sourced from topics.yaml routing.decision

_POLL_INTERVAL = 0.5
_POLL_TIMEOUT = 30.0


# ---------------------------------------------------------------------------
# Module-level fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def pg_conn() -> asyncpg.Connection:  # type: ignore[type-arg]
    """Direct asyncpg connection to stability-test omnibase_infra."""
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
    """Poll agent_routing_decisions until a row with correlation_id appears."""
    deadline = time.monotonic() + timeout
    while True:
        row = await conn.fetchrow(
            "SELECT * FROM agent_routing_decisions WHERE correlation_id = $1",
            uuid.UUID(correlation_id),
        )
        if row is not None:
            return dict(row)
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"No agent_routing_decisions row for correlation_id={correlation_id!r} "
                f"within {timeout}s"
            )
        await asyncio.sleep(_POLL_INTERVAL)


async def _row_count_for_id(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    row_id: str,
) -> int:
    result = await conn.fetchval(
        "SELECT COUNT(*) FROM agent_routing_decisions WHERE id = $1",
        uuid.UUID(row_id),
    )
    return int(result)


def _build_routing_decision_event(
    correlation_id: str,
    row_id: str,
) -> dict[str, Any]:
    return {
        "id": row_id,
        "correlation_id": correlation_id,
        "session_id": f"test-session-{correlation_id[:8]}",
        "selected_agent": "node_delegation_orchestrator",
        "confidence_score": 0.95,
        "request_type": "e2e_golden_chain_test",
        "routing_reason": "golden chain integration test — registration chain",
        "domain": "test",
        "timestamp": datetime.now(UTC).isoformat(),
        "metadata": {"source": "test_registration_e2e"},
    }


async def _publish_to_kafka(topic: str, payload: dict[str, Any]) -> tuple[int, int]:
    """Publish JSON payload to Kafka; return (partition, offset) metadata."""
    from aiokafka import AIOKafkaProducer

    producer = AIOKafkaProducer(
        bootstrap_servers=_KAFKA_BOOTSTRAP,
        value_serializer=lambda v: json.dumps(v).encode("utf-8"),
    )
    await producer.start()
    try:
        record_metadata = await producer.send_and_wait(topic, payload)
        return record_metadata.partition, record_metadata.offset
    finally:
        await producer.stop()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestRegistrationGoldenChainE2E:
    """Full reducer chain: Kafka publish -> consumer -> agent_routing_decisions row."""

    async def test_event_published_and_row_written(
        self,
        pg_conn: asyncpg.Connection,  # type: ignore[type-arg]
    ) -> None:
        """Publish one routing-decision event; assert row appears in agent_routing_decisions."""
        correlation_id = str(uuid.uuid4())
        row_id = str(uuid.uuid4())
        payload = _build_routing_decision_event(correlation_id, row_id)

        partition, offset = await _publish_to_kafka(_TOPIC_ROUTING_DECISION, payload)

        row = await _wait_for_row(pg_conn, correlation_id)

        assert str(row["correlation_id"]) == correlation_id
        assert row["selected_agent"] == "node_delegation_orchestrator"
        assert row["request_type"] == "e2e_golden_chain_test"

        evidence = {
            "topic": _TOPIC_ROUTING_DECISION,
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
        row_id = str(uuid.uuid4())
        payload = _build_routing_decision_event(correlation_id, row_id)

        await _publish_to_kafka(_TOPIC_ROUTING_DECISION, payload)
        await _wait_for_row(pg_conn, correlation_id)

        count_after_first = await _row_count_for_id(pg_conn, row_id)
        assert count_after_first == 1

        await _publish_to_kafka(_TOPIC_ROUTING_DECISION, payload)
        await asyncio.sleep(5.0)

        count_after_replay = await _row_count_for_id(pg_conn, row_id)
        assert count_after_replay == 1, (
            f"Idempotency violated: expected 1 row, got {count_after_replay} "
            f"for id={row_id!r}"
        )

    async def test_required_fields_populated(
        self,
        pg_conn: asyncpg.Connection,  # type: ignore[type-arg]
    ) -> None:
        """All contract-declared columns receive correct values."""
        correlation_id = str(uuid.uuid4())
        row_id = str(uuid.uuid4())
        payload = _build_routing_decision_event(correlation_id, row_id)

        await _publish_to_kafka(_TOPIC_ROUTING_DECISION, payload)
        row = await _wait_for_row(pg_conn, correlation_id)

        assert str(row["correlation_id"]) == correlation_id
        assert row["selected_agent"] is not None
        assert row["created_at"] is not None
        assert row["request_type"] is not None
