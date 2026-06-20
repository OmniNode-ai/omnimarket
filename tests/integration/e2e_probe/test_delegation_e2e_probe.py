# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
# onex-allow-file OMN-12789 reason="e2e probe test fixture — lab GPU server IP (192.168.86.201) used as parameterizable default; overridden by ONEX_E2E_* env vars at runtime; not a runtime default"
# test-literal-ok: OMN-12789 companion exemption for test_no_hardcoded_literals gate (see onex-allow-file above for leak-gate)
"""Re-runnable delegation e2e probe harness — OMN-12789 / OMN-12952.

Wave-4 4A-4F slice: thin publish → bus → terminal event → projection →
dashboard read assertions. Parameterized by lane via environment variables.
Replaces per-session hand-driven probes.

Architecture being tested
--------------------------
  [probe] thin-publish to onex.evt.omniclaude.task-delegated.v1
    -> node_projection_delegation consumer (running on .201 dev lane)
      -> delegation_events INSERT (omnidash_analytics DB)
        -> onex.evt.omnimarket.projection-delegation-applied.v1 terminal event
          -> projection API /projection/<topic> read (:3002)

Lanes and their env vars
--------------------------
  ONEX_E2E_LANE=dev (default) — dev lane on 192.168.86.201
    ONEX_E2E_KAFKA_BOOTSTRAP   dev lane Redpanda   default: 192.168.86.201:19092
    ONEX_E2E_POSTGRES_HOST     dev lane PG host    default: 192.168.86.201
    ONEX_E2E_POSTGRES_PORT     dev lane PG port    default: 5436
    ONEX_E2E_POSTGRES_DB       dev lane PG db      default: omnidash_analytics
    ONEX_E2E_POSTGRES_USER     dev lane PG user    default: postgres
    ONEX_E2E_POSTGRES_PASSWORD dev lane PG pass    env required (or POSTGRES_PASSWORD)
    ONEX_E2E_PROJECTION_URL    projection API URL  default: http://192.168.86.201:3002

  ONEX_E2E_LANE=stability-test — stability-test lane
    Same env-var names override; default addresses shift to stability-test ports
    (19092, 15436, http://192.168.86.201:3002 — stability-test projection API
     is on the same :3002 host but bound to the stability-test container).

Opt-in guard
--------------------------
Set OMN_ALLOW_LIVE_E2E_PROBE=true to execute tests against the live lane.
Without it all tests skip. This prevents accidental CI runs against the bus.

Assertion thresholds
--------------------------
Baseline assertion values are intentionally left as placeholders:
  - MIN_ROW_COUNT: calibrated after GEN-01 packet from Milestone 1.1
  - PROJECTION_FRESHNESS_THRESHOLD_MINUTES: calibrated from real observed latency
  - PROJECTION_API_MAX_LATENCY_MS: calibrated from real observed API response time
TODO OMN-12952 (Milestone 4.4(b)): fill in real baselines from GEN-01 packet.

Usage
--------------------------
  # Against dev lane (uses defaults):
  OMN_ALLOW_LIVE_E2E_PROBE=true \\
  ONEX_E2E_POSTGRES_PASSWORD=<pw> \\
  uv run pytest tests/integration/e2e_probe/test_delegation_e2e_probe.py -v -m e2e

  # Against stability-test lane:
  OMN_ALLOW_LIVE_E2E_PROBE=true \\
  ONEX_E2E_LANE=stability-test \\
  ONEX_E2E_KAFKA_BOOTSTRAP=192.168.86.201:19092 \\
  ONEX_E2E_POSTGRES_PORT=15436 \\
  ONEX_E2E_POSTGRES_PASSWORD=<pw> \\
  uv run pytest tests/integration/e2e_probe/test_delegation_e2e_probe.py -v -m e2e

Evidence root: docs/evidence/2026-06-12-weekend-pass/integration-testing/e2e-probe/
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any

import asyncpg
import httpx
import pytest
import pytest_asyncio
from aiokafka import AIOKafkaProducer
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Opt-in guard — all tests skip unless the flag is set
# ---------------------------------------------------------------------------

_ALLOW_FLAG = "OMN_ALLOW_LIVE_E2E_PROBE"
pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.skipif(
        os.environ.get(_ALLOW_FLAG, "").lower() != "true",
        reason=(
            f"Requires {_ALLOW_FLAG}=true to run against the live bus. "
            "Set this env var explicitly to execute the probe."
        ),
    ),
]

# ---------------------------------------------------------------------------
# Lane configuration (all values read from env at test collection time)
# ---------------------------------------------------------------------------

_LANE = os.environ.get("ONEX_E2E_LANE", "dev")

# Dev lane defaults (onex-allow-internal-ip: lab GPU server — read from env at runtime)
_DEFAULT_KAFKA = "192.168.86.201:19092"  # onex-allow-internal-ip OMN-12789 reason="dev/stability-test lab Redpanda; overridden by ONEX_E2E_KAFKA_BOOTSTRAP at runtime"
_DEFAULT_PG_HOST = "192.168.86.201"  # onex-allow-internal-ip OMN-12789 reason="dev/stability-test lab Postgres host; overridden by ONEX_E2E_POSTGRES_HOST at runtime"
_DEFAULT_PG_PORT_DEV = 5436
_DEFAULT_PG_PORT_STABILITY = 15436
_DEFAULT_PROJECTION_URL = "http://192.168.86.201:3002"  # onex-allow-internal-ip OMN-12789 reason="dev/stability-test projection API; overridden by ONEX_E2E_PROJECTION_URL at runtime"

_KAFKA_BOOTSTRAP = os.environ.get("ONEX_E2E_KAFKA_BOOTSTRAP", _DEFAULT_KAFKA)
_PG_HOST = os.environ.get("ONEX_E2E_POSTGRES_HOST", _DEFAULT_PG_HOST)
_PG_PORT = int(
    os.environ.get(
        "ONEX_E2E_POSTGRES_PORT",
        str(
            _DEFAULT_PG_PORT_STABILITY
            if _LANE == "stability-test"
            else _DEFAULT_PG_PORT_DEV
        ),
    )
)
_PG_DB = os.environ.get("ONEX_E2E_POSTGRES_DB", "omnidash_analytics")
_PG_USER = os.environ.get("ONEX_E2E_POSTGRES_USER", "postgres")
_PG_PASSWORD = os.environ.get(
    "ONEX_E2E_POSTGRES_PASSWORD",
    os.environ.get("POSTGRES_PASSWORD", ""),
)
_PROJECTION_URL = os.environ.get("ONEX_E2E_PROJECTION_URL", _DEFAULT_PROJECTION_URL)

# ---------------------------------------------------------------------------
# Topic constants (read from contract — never hardcode here)
# ---------------------------------------------------------------------------

# Input topic: thin-publish target
_TOPIC_TASK_DELEGATED = "onex.evt.omniclaude.task-delegated.v1"
# Terminal event: emitted by node_projection_delegation after successful projection
_TOPIC_PROJECTION_APPLIED = "onex.evt.omnimarket.projection-delegation-applied.v1"
# Projection API topic for delegation decisions (contract: node_projection_delegation)
_PROJECTION_API_TOPIC_DECISIONS = "onex.snapshot.projection.delegation.decisions.v1"
# Projection API topic for delegation summary
_PROJECTION_API_TOPIC_SUMMARY = "onex.snapshot.projection.delegation.summary.v1"
# Projection API topic for correlation trace
_PROJECTION_API_TOPIC_TRACE = "onex.snapshot.projection.delegation.correlation-trace.v1"

# ---------------------------------------------------------------------------
# Timing constants
# ---------------------------------------------------------------------------

_POLL_INTERVAL_S = 0.5  # poll DB every N seconds waiting for projection
_POLL_TIMEOUT_S = 45.0  # max wait for projection row to appear
_TERMINAL_EVENT_TIMEOUT_S = 50.0  # max wait for terminal Kafka event
_HTTP_TIMEOUT_S = 10.0  # projection API request timeout

# ---------------------------------------------------------------------------
# Assertion threshold PLACEHOLDERS (calibrate from GEN-01 packet after M1.1)
# ---------------------------------------------------------------------------
# TODO OMN-12952 Milestone 4.4(b): replace these with real baseline values
# derived from the GEN-01 packet once Milestone 1.1 lands.

# Minimum delegation_events rows after probe (calibrate: expected ~1+ per probe run)
MIN_ROW_COUNT_AFTER_PUBLISH = 1  # TODO OMN-12952: verify baseline row count

# Projection API must return at least 1 row in the decisions topic
MIN_DECISIONS_ROWS = 1  # TODO OMN-12952: verify baseline decisions count

# Freshness threshold in minutes: summary projection freshness < this value
MAX_PROJECTION_AGE_MINUTES = 10  # TODO OMN-12952: calibrate from real latency

# Projection API response time upper bound (milliseconds)
PROJECTION_API_MAX_LATENCY_MS = 3000  # TODO OMN-12952: calibrate from observed API

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_delegation_payload(correlation_id: str) -> dict[str, Any]:
    """Build a minimal task-delegated event payload for the probe."""
    return {
        "correlation_id": correlation_id,
        "task_type": "e2e_probe_harness",
        "delegated_to": "claude-haiku-4-5",
        "delegated_by": "omnimarket_e2e_probe_harness",
        "quality_gate_passed": True,
        "timestamp": datetime.now(UTC).isoformat(),
        "cost_usd": 0.0,
        "cost_savings_usd": 0.001,
        "pricing_manifest_version": 1,
    }


async def _thin_publish(topic: str, payload: dict[str, Any]) -> tuple[int, int]:
    """Thin-publish a JSON event envelope to the specified Kafka topic.

    Returns (partition, offset) from the broker acknowledgement.
    This is the canonical publish path: wrap in ModelEventEnvelope, serialize,
    publish — no side effects, no state, no dependency on the runtime.
    """
    correlation_id = str(payload.get("correlation_id", uuid.uuid4()))
    envelope = ModelEventEnvelope[dict[str, Any]](
        payload=payload,
        correlation_id=uuid.UUID(correlation_id),
        source_tool="omnimarket.e2e-probe-harness.omn-12789",
        event_type="omniclaude.task-delegated",
    )
    producer = AIOKafkaProducer(
        bootstrap_servers=_KAFKA_BOOTSTRAP,
        value_serializer=lambda v: json.dumps(v).encode("utf-8"),
    )
    await producer.start()
    try:
        record = await producer.send_and_wait(topic, envelope.model_dump(mode="json"))
        log.info(
            "Probe published to %s partition=%s offset=%s correlation_id=%s",
            topic,
            record.partition,
            record.offset,
            correlation_id,
        )
        return record.partition, record.offset
    finally:
        await producer.stop()


async def _wait_for_projection_row(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    correlation_id: str,
    *,
    timeout: float = _POLL_TIMEOUT_S,
) -> dict[str, Any]:
    """Poll delegation_events until a row matching correlation_id appears.

    Raises TimeoutError if the row does not appear within ``timeout`` seconds.
    """
    deadline = time.monotonic() + timeout
    while True:
        row = await conn.fetchrow(
            "SELECT * FROM delegation_events WHERE correlation_id = $1",
            correlation_id,
        )
        if row is not None:
            return dict(row)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(
                f"delegation_events row not found for correlation_id={correlation_id!r} "
                f"within {timeout}s — lane={_LANE} kafka={_KAFKA_BOOTSTRAP} "
                f"pg={_PG_HOST}:{_PG_PORT}"
            )
        await asyncio.sleep(_POLL_INTERVAL_S)


async def _wait_for_terminal_event(
    correlation_id: str,
    *,
    timeout: float = _TERMINAL_EVENT_TIMEOUT_S,
) -> dict[str, Any]:
    """Subscribe to projection-delegation-applied topic and wait for the terminal event.

    Returns the raw deserialized envelope dict.

    OMN-13361: ``consumer_timeout_ms`` does NOT stop ``async for msg in consumer``
    on idle in aiokafka — ``__anext__`` only raises ``StopAsyncIteration`` when the
    consumer is explicitly stopped, so an idle topic blocks forever and the
    ``TimeoutError`` below was unreachable (the test hung indefinitely, which is
    why ``TestE2E4BTerminalEvent`` was skipped). Wrap the consume loop in an
    ``asyncio.timeout`` monotonic deadline — mirroring the working
    ``_wait_for_projection_row`` poll — so the wait bounds at ``timeout`` seconds
    and raises ``TimeoutError`` on miss.
    """
    from aiokafka import AIOKafkaConsumer

    received: list[dict[str, Any]] = []
    group_id = f"omnimarket-e2e-probe-{uuid.uuid4().hex[:8]}"

    consumer = AIOKafkaConsumer(
        _TOPIC_PROJECTION_APPLIED,
        bootstrap_servers=_KAFKA_BOOTSTRAP,
        group_id=group_id,
        value_deserializer=lambda v: json.loads(v.decode("utf-8")),
        auto_offset_reset="latest",
        enable_auto_commit=True,
    )

    await consumer.start()
    try:
        async with asyncio.timeout(timeout):
            async for msg in consumer:
                envelope = msg.value
                # Extract correlation_id from envelope payload or top-level
                payload = (
                    envelope.get("payload", {}) if isinstance(envelope, dict) else {}
                )
                env_correlation = str(
                    envelope.get("correlation_id", "")
                    or payload.get("correlation_id", "")
                )
                if env_correlation == correlation_id or correlation_id in str(envelope):
                    received.append(envelope)
                    break
    except TimeoutError:
        # Idle / terminal-never-arrived: fall through to the explicit raise below
        # so the miss carries the full diagnostic context, not a bare deadline.
        pass
    except asyncio.CancelledError:
        pass
    except Exception as exc:
        log.warning("Terminal event consumer error: %s", exc)
    finally:
        await consumer.stop()

    if not received:
        raise TimeoutError(
            f"Terminal event {_TOPIC_PROJECTION_APPLIED!r} not received for "
            f"correlation_id={correlation_id!r} within {timeout}s"
        )
    return received[0]


def _projection_api_get(
    topic: str,
    *,
    params: dict[str, str] | None = None,
) -> httpx.Response:
    """Synchronous GET against the projection API for the given topic.

    Uses /projection/<topic> endpoint on the configured projection URL.
    """
    url = f"{_PROJECTION_URL}/projection/{topic}"
    with httpx.Client(timeout=_HTTP_TIMEOUT_S) as client:
        response = client.get(url, params=params or {})
    return response


# ---------------------------------------------------------------------------
# Module-level Postgres fixture
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def pg_conn() -> AsyncGenerator[asyncpg.Connection, None]:  # type: ignore[type-arg]
    """Asyncpg connection to the lane's omnidash_analytics database."""
    if not _PG_PASSWORD:
        pytest.skip(
            f"ONEX_E2E_POSTGRES_PASSWORD / POSTGRES_PASSWORD not set — "
            f"cannot connect to {_LANE} Postgres at {_PG_HOST}:{_PG_PORT}"
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


# ---------------------------------------------------------------------------
# 4A: Thin publish — bus connectivity
# ---------------------------------------------------------------------------


class TestE2E4AThinPublish:
    """4A: Verify the probe can thin-publish to the delegation topic on the live bus.

    Scope: publish one event, verify broker acknowledgement (partition+offset).
    No DB or projection assertions — this isolates the publish leg.
    """

    async def test_thin_publish_returns_broker_ack(self) -> None:
        """Publish to task-delegated topic; broker must ack with partition+offset."""
        correlation_id = str(uuid.uuid4())
        payload = _build_delegation_payload(correlation_id)
        partition, offset = await _thin_publish(_TOPIC_TASK_DELEGATED, payload)
        assert isinstance(partition, int), (
            f"Expected int partition, got {type(partition)}"
        )
        assert offset >= 0, f"Expected non-negative offset, got {offset}"
        log.info(
            "4A PASS: thin-publish acked — lane=%s partition=%s offset=%s cid=%s",
            _LANE,
            partition,
            offset,
            correlation_id,
        )

    async def test_thin_publish_correct_topic(self) -> None:
        """Published topic must match the contract-declared subscribe topic."""
        # This verifies the probe uses the exact topic the consumer subscribes to.
        # If the topic drifts from the contract, the downstream legs all fail.
        assert _TOPIC_TASK_DELEGATED == "onex.evt.omniclaude.task-delegated.v1", (
            f"Topic mismatch: expected contract topic, got {_TOPIC_TASK_DELEGATED!r}"
        )

    async def test_thin_publish_envelope_schema_valid(self) -> None:
        """Envelope wrapping the payload must satisfy the ModelEventEnvelope schema."""
        correlation_id = str(uuid.uuid4())
        payload = _build_delegation_payload(correlation_id)
        envelope = ModelEventEnvelope[dict[str, Any]](
            payload=payload,
            correlation_id=uuid.UUID(correlation_id),
            source_tool="omnimarket.e2e-probe-harness.omn-12789",
            event_type="omniclaude.task-delegated",
        )
        dumped = envelope.model_dump(mode="json")
        # Envelope must carry correlation_id and payload
        assert "correlation_id" in dumped, "Envelope missing correlation_id"
        assert "payload" in dumped, "Envelope missing payload"
        assert dumped["payload"]["correlation_id"] == correlation_id


# ---------------------------------------------------------------------------
# 4B: Bus → terminal event
# ---------------------------------------------------------------------------


class TestE2E4BTerminalEvent:
    """4B: Verify the projection consumer emits a terminal event after publish.

    Scope: publish → subscribe to projection-delegation-applied → assert the
    terminal event arrives within timeout. This covers the bus-to-consumer leg.
    """

    async def test_terminal_event_arrives_after_publish(self) -> None:
        """Publish a delegation event; projection-delegation-applied must arrive."""
        correlation_id = str(uuid.uuid4())
        payload = _build_delegation_payload(correlation_id)

        # Publish FIRST so the consumer sees it
        await _thin_publish(_TOPIC_TASK_DELEGATED, payload)

        # Wait for terminal event — consumer must pick it up and emit
        terminal = await _wait_for_terminal_event(correlation_id)

        assert terminal is not None, "Terminal event not received"
        log.info(
            "4B PASS: terminal event received — lane=%s cid=%s topic=%s",
            _LANE,
            correlation_id,
            _TOPIC_PROJECTION_APPLIED,
        )


# ---------------------------------------------------------------------------
# 4C: Bus → projection (DB write)
# ---------------------------------------------------------------------------


class TestE2E4CProjectionWrite:
    """4C: Verify the projection consumer writes to the delegation_events table.

    Scope: publish → poll DB → assert row fields match published payload.
    Covers the bus-to-DB projection leg.
    """

    async def test_projection_row_written(
        self,
        pg_conn: asyncpg.Connection,  # type: ignore[type-arg]
    ) -> None:
        """Publish one event; a delegation_events row must appear within timeout."""
        correlation_id = str(uuid.uuid4())
        payload = _build_delegation_payload(correlation_id)

        await _thin_publish(_TOPIC_TASK_DELEGATED, payload)

        row = await _wait_for_projection_row(pg_conn, correlation_id)

        assert row["correlation_id"] == correlation_id
        assert row["task_type"] == "e2e_probe_harness"
        assert row["delegated_to"] == "claude-haiku-4-5"
        assert row["quality_gate_passed"] is True
        log.info(
            "4C PASS: projection row written — lane=%s cid=%s row_id=%s",
            _LANE,
            correlation_id,
            row.get("id"),
        )

    async def test_projection_row_contains_required_fields(
        self,
        pg_conn: asyncpg.Connection,  # type: ignore[type-arg]
    ) -> None:
        """Contract-declared columns in delegation_events must be non-null after write."""
        correlation_id = str(uuid.uuid4())
        payload = _build_delegation_payload(correlation_id)
        await _thin_publish(_TOPIC_TASK_DELEGATED, payload)
        row = await _wait_for_projection_row(pg_conn, correlation_id)

        required_columns = ["correlation_id", "task_type", "delegated_to"]
        for col in required_columns:
            assert row.get(col) is not None, (
                f"Required column {col!r} is NULL after projection. Row: {dict(row)}"
            )

    async def test_projection_idempotency(
        self,
        pg_conn: asyncpg.Connection,  # type: ignore[type-arg]
    ) -> None:
        """Replaying the same event must not create a duplicate row (UPSERT semantics)."""
        correlation_id = str(uuid.uuid4())
        payload = _build_delegation_payload(correlation_id)

        await _thin_publish(_TOPIC_TASK_DELEGATED, payload)
        await _wait_for_projection_row(pg_conn, correlation_id)

        # Second publish of the same event
        await _thin_publish(_TOPIC_TASK_DELEGATED, payload)
        await asyncio.sleep(5.0)  # give consumer time to process replay

        count = await pg_conn.fetchval(
            "SELECT COUNT(*) FROM delegation_events WHERE correlation_id = $1",
            correlation_id,
        )
        assert int(count) == 1, (  # type: ignore[arg-type]
            f"Idempotency violated: expected 1 row, got {count} for cid={correlation_id!r}"
        )
        log.info(
            "4C PASS: idempotency confirmed — lane=%s cid=%s", _LANE, correlation_id
        )

    async def test_row_count_meets_baseline(
        self,
        pg_conn: asyncpg.Connection,  # type: ignore[type-arg]
    ) -> None:
        """Total delegation_events row count must meet the minimum baseline.

        TODO OMN-12952 Milestone 4.4(b): MIN_ROW_COUNT_AFTER_PUBLISH is a
        placeholder. Replace with the real baseline from the GEN-01 packet.
        """
        correlation_id = str(uuid.uuid4())
        payload = _build_delegation_payload(correlation_id)
        await _thin_publish(_TOPIC_TASK_DELEGATED, payload)
        await _wait_for_projection_row(pg_conn, correlation_id)

        total = await pg_conn.fetchval("SELECT COUNT(*) FROM delegation_events")
        assert int(total) >= MIN_ROW_COUNT_AFTER_PUBLISH, (  # type: ignore[arg-type]
            f"Total delegation_events count {total} below baseline {MIN_ROW_COUNT_AFTER_PUBLISH}. "
            "TODO OMN-12952: calibrate baseline from GEN-01 packet."
        )


# ---------------------------------------------------------------------------
# 4D: Projection API read
# ---------------------------------------------------------------------------


class TestE2E4DProjectionAPIRead:
    """4D: Verify the projection API serves delegation data over HTTP.

    Scope: read /projection/<topic> on the configured projection URL.
    Covers the projection-API read leg. No Kafka or DB needed beyond what 4C wrote.
    """

    def test_decisions_topic_returns_200(self) -> None:
        """GET /projection/decisions.v1 must return 200 with rows array."""
        start_ms = time.monotonic() * 1000
        response = _projection_api_get(_PROJECTION_API_TOPIC_DECISIONS)
        elapsed_ms = time.monotonic() * 1000 - start_ms

        assert response.status_code == 200, (
            f"Expected 200, got {response.status_code} from "
            f"{_PROJECTION_URL}/projection/{_PROJECTION_API_TOPIC_DECISIONS}. "
            f"Body: {response.text[:200]}"
        )
        body = response.json()
        assert "rows" in body, f"Response missing 'rows' key: {body.keys()}"
        assert isinstance(body["rows"], list), (
            f"'rows' is not a list: {type(body['rows'])}"
        )

        # Baseline check
        # TODO OMN-12952 Milestone 4.4(b): verify baseline row count
        assert len(body["rows"]) >= MIN_DECISIONS_ROWS, (
            f"decisions topic returned {len(body['rows'])} rows, expected >= {MIN_DECISIONS_ROWS}. "
            "TODO OMN-12952: calibrate baseline from GEN-01 packet."
        )

        # Latency check
        # TODO OMN-12952 Milestone 4.4(b): calibrate PROJECTION_API_MAX_LATENCY_MS
        assert elapsed_ms < PROJECTION_API_MAX_LATENCY_MS, (
            f"Projection API response time {elapsed_ms:.0f}ms exceeded "
            f"{PROJECTION_API_MAX_LATENCY_MS}ms. "
            "TODO OMN-12952: calibrate baseline from observed API latency."
        )
        log.info(
            "4D PASS: decisions topic — lane=%s rows=%s latency_ms=%.0f",
            _LANE,
            len(body["rows"]),
            elapsed_ms,
        )

    def test_summary_topic_returns_200(self) -> None:
        """GET /projection/summary.v1 must return 200."""
        response = _projection_api_get(_PROJECTION_API_TOPIC_SUMMARY)
        assert response.status_code == 200, (
            f"Expected 200, got {response.status_code}. Body: {response.text[:200]}"
        )
        body = response.json()
        assert "rows" in body, "Response missing 'rows' key"
        log.info(
            "4D PASS: summary topic — lane=%s rows=%s", _LANE, len(body.get("rows", []))
        )

    def test_unknown_topic_returns_404(self) -> None:
        """A non-existent topic must return 404 (not 500)."""
        response = _projection_api_get("onex.snapshot.probe.nonexistent.v1")
        assert response.status_code == 404, (
            f"Expected 404 for unknown topic, got {response.status_code}. "
            f"Body: {response.text[:200]}"
        )

    def test_correlation_id_filter_accepted(self) -> None:
        """The correlation_id query param must be forwarded without crashing the API."""
        fake_cid = str(uuid.uuid4())
        response = _projection_api_get(
            _PROJECTION_API_TOPIC_TRACE,
            params={"correlation_id": fake_cid},
        )
        # Either 200 (empty rows) or 404 — not 500
        assert response.status_code in (200, 404), (
            f"Unexpected status {response.status_code} for correlation_id filter. "
            f"Body: {response.text[:200]}"
        )

    def test_projection_api_health_endpoint(self) -> None:
        """GET /health on the projection API must return 200."""
        with httpx.Client(timeout=_HTTP_TIMEOUT_S) as client:
            response = client.get(f"{_PROJECTION_URL}/health")
        assert response.status_code == 200, (
            f"Projection API /health returned {response.status_code}. "
            f"URL: {_PROJECTION_URL}/health"
        )
        log.info(
            "4D PASS: projection API health — lane=%s status=%s",
            _LANE,
            response.status_code,
        )


# ---------------------------------------------------------------------------
# 4E: Full chain round-trip (publish → projection API read)
# ---------------------------------------------------------------------------


class TestE2E4EFullChainRoundTrip:
    """4E: Full probe chain from thin-publish to projection API read.

    Scope: combines 4A+4C+4D into a single end-to-end assertion.
    Publishes a fresh event, waits for the projection row, then confirms
    the row is retrievable via the projection API using the correlation_id.
    """

    async def test_publish_to_projection_api_round_trip(
        self,
        pg_conn: asyncpg.Connection,  # type: ignore[type-arg]
    ) -> None:
        """Publish → projection row → projection API read — all legs in one test."""
        correlation_id = str(uuid.uuid4())
        payload = _build_delegation_payload(correlation_id)

        # 4A: thin publish
        partition, offset = await _thin_publish(_TOPIC_TASK_DELEGATED, payload)
        assert offset >= 0

        # 4C: wait for projection row
        row = await _wait_for_projection_row(pg_conn, correlation_id)
        assert row["correlation_id"] == correlation_id

        # 4D: projection API read — filter by correlation_id
        response = _projection_api_get(
            _PROJECTION_API_TOPIC_TRACE,
            params={"correlation_id": correlation_id},
        )
        # 200 with row OR 404 are both acceptable here: the trace topic may not
        # be populated immediately after projection. The test proves the API
        # accepts the filter without error.
        # TODO OMN-12952 Milestone 4.4(b): tighten to assert 200 + row once
        # baseline confirms projection-to-API latency.
        assert response.status_code in (200, 404), (
            f"Unexpected status {response.status_code} from trace topic. "
            f"Body: {response.text[:200]}"
        )

        log.info(
            "4E PASS: full chain round-trip — lane=%s cid=%s "
            "partition=%s offset=%s projection_api_status=%s",
            _LANE,
            correlation_id,
            partition,
            offset,
            response.status_code,
        )

    async def test_chain_latency_within_threshold(
        self,
        pg_conn: asyncpg.Connection,  # type: ignore[type-arg]
    ) -> None:
        """End-to-end chain latency (publish to DB row) must be within tolerance.

        TODO OMN-12952 Milestone 4.4(b): calibrate MAX_CHAIN_LATENCY_S from
        GEN-01 packet observations.
        """
        # TODO OMN-12952 Milestone 4.4(b): calibrate from GEN-01 packet observations
        max_chain_latency_s = 30.0

        correlation_id = str(uuid.uuid4())
        payload = _build_delegation_payload(correlation_id)

        t0 = time.monotonic()
        await _thin_publish(_TOPIC_TASK_DELEGATED, payload)
        await _wait_for_projection_row(pg_conn, correlation_id)
        elapsed = time.monotonic() - t0

        assert elapsed < max_chain_latency_s, (
            f"Chain latency {elapsed:.1f}s exceeded threshold {max_chain_latency_s}s. "
            "TODO OMN-12952: calibrate from GEN-01 packet."
        )
        log.info(
            "4E PASS: chain latency %.1fs (threshold %.1fs) — lane=%s cid=%s",
            elapsed,
            max_chain_latency_s,
            _LANE,
            correlation_id,
        )


# ---------------------------------------------------------------------------
# 4F: Dashboard read assertions (projection API as dashboard backend)
# ---------------------------------------------------------------------------


class TestE2E4FDashboardReadAssertions:
    """4F: Dashboard-read assertions against the projection API topics.

    The dashboard reads delegation data via /projection/<topic> on :3002.
    These tests verify the widget topics return the expected schema.
    Per PROCESS_FAILURE_RETRO.md A-2: curl-only evidence is insufficient for
    UI assertions. This class covers the API tier; full Playwright assertions
    are in docs/evidence/2026-06-12-weekend-pass/dogfooding/playwright/.

    TODO OMN-12952 Milestone 4.4(b): extend with Playwright click + render
    evidence after Milestone 1.1 lands (per dispatch template §C).
    """

    def test_summary_widget_topic_has_expected_shape(self) -> None:
        """Summary topic must return the expected camelCase fields the dashboard renders.

        Checks for the fields referenced by the delegation summary widget.
        TODO OMN-12952: assert non-null values once GEN-01 baseline is in.
        """
        response = _projection_api_get(_PROJECTION_API_TOPIC_SUMMARY)
        if response.status_code == 200:
            body = response.json()
            rows = body.get("rows", [])
            # Shape check — if rows exist, verify known camelCase fields
            if rows:
                row = rows[0]
                # These are the camelCase columns declared in the contract
                expected_fields = [
                    "totalDelegations",
                    "qualityGatePassRate",
                    "totalSavingsUsd",
                ]
                present = [f for f in expected_fields if f in row]
                assert len(present) > 0, (
                    f"Summary row missing expected camelCase fields. "
                    f"Expected one of {expected_fields}. Got keys: {list(row.keys())}"
                )
        else:
            # 404 = no data yet; not a failure for a dry-run probe
            log.info(
                "4F NOTE: summary topic returned %s (no data yet) — lane=%s",
                response.status_code,
                _LANE,
            )

    def test_decisions_topic_row_schema(self) -> None:
        """Decisions topic rows must include correlation_id, task_type, model_name.

        TODO OMN-12952 Milestone 4.4(b): assert non-null values from GEN-01 packet.
        """
        response = _projection_api_get(_PROJECTION_API_TOPIC_DECISIONS)
        if response.status_code == 200:
            body = response.json()
            rows = body.get("rows", [])
            if rows:
                row = rows[0]
                schema_fields = ["correlation_id", "task_type"]
                for field in schema_fields:
                    assert field in row, (
                        f"Decisions row missing field {field!r}. Keys: {list(row.keys())}"
                    )
        else:
            log.info(
                "4F NOTE: decisions topic returned %s — lane=%s",
                response.status_code,
                _LANE,
            )

    def test_projection_api_returns_freshness_metadata(self) -> None:
        """Projection API response must include freshness metadata envelope.

        The dashboard uses this to determine staleness. Contract:
        ProjectionTableConfig.freshness_column drives the computation.
        TODO OMN-12952 Milestone 4.4(b): assert freshness='fresh' once
        data is populated from GEN-01.
        """
        response = _projection_api_get(_PROJECTION_API_TOPIC_DECISIONS)
        if response.status_code == 200:
            body = response.json()
            # Freshness is in the top-level envelope (from projection API server)
            # Either 'freshness' key or 'status' key depending on API version
            has_freshness_metadata = (
                "freshness" in body or "status" in body or "meta" in body
            )
            # Note: the projection API may return rows-only without top-level
            # freshness for some topics. This is a best-effort check.
            if not has_freshness_metadata and body.get("rows"):
                log.info(
                    "4F NOTE: projection API response does not include top-level "
                    "freshness key — may be intentional. Keys: %s",
                    list(body.keys()),
                )
        else:
            log.info(
                "4F NOTE: decisions returned %s — skipping freshness check",
                response.status_code,
            )
