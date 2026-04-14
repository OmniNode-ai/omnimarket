# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared test fixtures for omnimarket golden chain and integration tests."""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import AsyncGenerator, Callable, Generator
from typing import Any
from urllib.parse import quote_plus

import asyncpg
import pytest
import pytest_asyncio
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_infra.event_bus.event_bus_kafka import EventBusKafka
from omnibase_infra.event_bus.models.config import ModelKafkaEventBusConfig


@pytest.fixture
def event_bus() -> EventBusInmemory:
    """Create a fresh in-memory event bus for testing."""
    return EventBusInmemory(environment="test", group="omnimarket-test")


# ---------------------------------------------------------------------------
# Integration fixtures (only active under @pytest.mark.integration)
# ---------------------------------------------------------------------------

_POSTGRES_HOST = os.environ.get("INTEGRATION_POSTGRES_HOST", "192.168.86.201")
_POSTGRES_PORT = int(os.environ.get("INTEGRATION_POSTGRES_PORT", "5436"))
_POSTGRES_USER = os.environ.get("INTEGRATION_POSTGRES_USER", "postgres")
_POSTGRES_PASSWORD = os.environ.get(
    "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
)
_POSTGRES_DB = os.environ.get("INTEGRATION_POSTGRES_DB", "omnibase_infra")

_KAFKA_BOOTSTRAP = os.environ.get("KAFKA_BOOTSTRAP_SERVERS", "localhost:19092")


def _integration_dsn() -> str:
    return (
        f"postgresql://{quote_plus(_POSTGRES_USER)}:{quote_plus(_POSTGRES_PASSWORD)}"
        f"@{_POSTGRES_HOST}:{_POSTGRES_PORT}/{_POSTGRES_DB}"
    )


@pytest_asyncio.fixture
async def postgres_fixture(
    request: pytest.FixtureRequest,
) -> AsyncGenerator[asyncpg.Connection, None]:
    """Real asyncpg connection to 192.168.86.201:5436.

    Skips automatically when not under @pytest.mark.integration or when
    POSTGRES_PASSWORD is unset (CI without .env).
    """
    if not request.node.get_closest_marker("integration"):
        pytest.skip("postgres_fixture requires @pytest.mark.integration")
    if not _POSTGRES_PASSWORD:
        pytest.skip("POSTGRES_PASSWORD not set — skipping integration postgres fixture")
    conn: asyncpg.Connection = await asyncpg.connect(_integration_dsn())
    try:
        yield conn
    finally:
        await conn.close()


@pytest.fixture
def integration_event_bus() -> Generator[EventBusInmemory, None, None]:
    """Fresh EventBusInmemory scoped to an integration test.

    Provides the same interface as event_bus but named distinctly so tests
    can assert bus.published after handler invocation.
    """
    bus = EventBusInmemory(
        environment="integration-test", group="omnimarket-integration"
    )
    return bus


@pytest_asyncio.fixture
async def kafka_integration_bus(
    request: pytest.FixtureRequest,
) -> AsyncGenerator[EventBusKafka, None]:
    """Real Kafka-backed event bus wired to KAFKA_BOOTSTRAP_SERVERS.

    Defaults to localhost:19092 (matches docker-compose.e2e.yml Redpanda port).
    Skips automatically when not under @pytest.mark.integration.

    Topic auto-creation is handled by the e2e compose redpanda-topic-manager
    service. For ad-hoc topics used in tests, callers should publish with
    auto.create.topics.enable (Redpanda default: on).
    """
    if not request.node.get_closest_marker("integration"):
        pytest.skip("kafka_integration_bus requires @pytest.mark.integration")

    config = ModelKafkaEventBusConfig(
        bootstrap_servers=_KAFKA_BOOTSTRAP,
        environment="integration-test",
        timeout_seconds=10,
        max_retry_attempts=1,
        retry_backoff_base=0.1,
        circuit_breaker_threshold=5,
        circuit_breaker_reset_timeout=30.0,
        consumer_sleep_interval=0.05,
        enable_idempotence=False,
        auto_offset_reset="earliest",
        enable_auto_commit=True,
        dead_letter_topic=None,
        instance_id=None,
        reconnect_backoff_ms=500,
        reconnect_backoff_max_ms=2000,
    )
    bus = EventBusKafka(config=config)
    await bus.start()
    try:
        yield bus
    finally:
        await bus.close()


async def wait_for_db_row(
    conn: asyncpg.Connection,
    table: str,
    predicate: Callable[[dict[str, Any]], bool],
    *,
    timeout: float = 30.0,
    poll_interval: float = 0.25,
) -> dict[str, Any]:
    """Poll a Postgres table until a row matching predicate appears.

    Args:
        conn: asyncpg connection (from postgres_fixture)
        table: Unqualified table name to query
        predicate: Callable that receives a row dict and returns True when found
        timeout: Maximum seconds to wait before raising TimeoutError
        poll_interval: Seconds between polls

    Returns:
        First matching row as a dict

    Raises:
        TimeoutError: If no matching row appears within timeout seconds
    """
    deadline = time.monotonic() + timeout
    while True:
        rows = await conn.fetch(f"SELECT * FROM {table}")
        for row in rows:
            row_dict = dict(row)
            if predicate(row_dict):
                return row_dict
        if time.monotonic() >= deadline:
            raise TimeoutError(f"No matching row found in {table!r} within {timeout}s")
        await asyncio.sleep(poll_interval)


# ---------------------------------------------------------------------------
# Lint guard: reject EventBusInmemory imports in tests/integration/*
# ---------------------------------------------------------------------------


def pytest_collect_file(parent: pytest.Collector, file_path: Any) -> None:
    """Block any integration test that imports EventBusInmemory."""
    import ast

    path_str = str(file_path)
    if "/tests/integration/" not in path_str or not path_str.endswith(".py"):
        return

    try:
        source = file_path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=path_str)
    except (OSError, SyntaxError):
        return

    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ImportFrom)
            and node.names
            and any(alias.name == "EventBusInmemory" for alias in node.names)
        ):
            pytest.fail(
                f"[OMN-8726] {path_str} imports EventBusInmemory — "
                "integration tests must use kafka_integration_bus fixture, "
                "not the in-memory bus."
            )
