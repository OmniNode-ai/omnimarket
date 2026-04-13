"""Shared test fixtures for omnimarket golden chain and integration tests."""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator, Generator
from urllib.parse import quote_plus

import asyncpg
import pytest
import pytest_asyncio
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory


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
