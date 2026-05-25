# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Unit tests for NodeLogPersistenceEffect.

No real database — asyncpg pool is mocked throughout.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from omnimarket.nodes.node_log_persistence_effect.handlers.handler_log_persistence_effect import (
    ModelLogPersistenceResult,
    NodeLogPersistenceEffect,
)
from omnimarket.nodes.node_log_projection.handlers.handler_log_projection import (
    EnumLogLevel,
    ModelLogEntry,
)


def _make_entry(**kwargs: Any) -> ModelLogEntry:
    defaults: dict[str, Any] = {
        "node_name": "test_node",
        "message": "hello world",
    }
    defaults.update(kwargs)
    return ModelLogEntry(**defaults)


def _make_pool(fetchval_return: Any = "some-entry-id") -> MagicMock:
    """Build a mock asyncpg pool whose conn.fetchval returns fetchval_return."""
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=fetchval_return)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_AsyncContextManager(conn))
    return pool, conn


class _AsyncContextManager:
    """Minimal async context manager wrapping a value."""

    def __init__(self, value: Any) -> None:
        self._value = value

    async def __aenter__(self) -> Any:
        return self._value

    async def __aexit__(self, *args: Any) -> None:
        pass


# ---------------------------------------------------------------------------
# Deserialization
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_model_log_entry_round_trip() -> None:
    entry = _make_entry(
        node_name="my_node",
        function_name="do_thing",
        level=EnumLogLevel.ERROR,
        message="boom",
        correlation_id="corr-123",
        duration_ms=42.5,
        metadata={"key": "val"},
    )
    assert entry.node_name == "my_node"
    assert entry.level == EnumLogLevel.ERROR
    assert entry.correlation_id == "corr-123"
    assert entry.metadata == {"key": "val"}


# ---------------------------------------------------------------------------
# Graceful degradation — no pool
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handle_skips_when_pool_is_none() -> None:
    handler = NodeLogPersistenceEffect(pool=None, pg_dsn="")
    entry = _make_entry()

    result = await handler.handle(entry)

    assert isinstance(result, ModelLogPersistenceResult)
    assert result.status == "skipped"
    assert result.entry_id == entry.entry_id


# ---------------------------------------------------------------------------
# Successful insert
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handle_inserts_correct_params() -> None:
    entry = _make_entry(
        node_name="my_node",
        function_name="fn",
        level=EnumLogLevel.WARNING,
        message="watch out",
        correlation_id="c-1",
        duration_ms=10.0,
        metadata={"env": "test"},
    )
    pool, conn = _make_pool(fetchval_return=entry.entry_id)
    handler = NodeLogPersistenceEffect(pool=pool)

    result = await handler.handle(entry)

    assert result.status == "written"
    assert result.entry_id == entry.entry_id

    conn.fetchval.assert_awaited_once()
    call_args = conn.fetchval.call_args
    positional = call_args.args

    assert positional[1] == entry.entry_id
    assert positional[2] == entry.timestamp
    assert positional[3] == entry.node_name
    assert positional[4] == entry.function_name
    assert positional[5] == entry.level.value
    assert positional[6] == entry.message
    assert positional[7] == entry.correlation_id
    assert positional[8] == entry.duration_ms
    assert json.loads(positional[9]) == entry.metadata


# ---------------------------------------------------------------------------
# Idempotent insert (conflict — row already exists)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handle_idempotent_when_conflict() -> None:
    entry = _make_entry()
    pool, _conn = _make_pool(fetchval_return=None)
    handler = NodeLogPersistenceEffect(pool=pool)

    result = await handler.handle(entry)

    assert result.status == "idempotent"
    assert result.entry_id == entry.entry_id


# ---------------------------------------------------------------------------
# DB error — returns error status, does not raise
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handle_returns_error_on_db_exception() -> None:
    entry = _make_entry()
    conn = AsyncMock()
    conn.fetchval = AsyncMock(side_effect=RuntimeError("DB is down"))
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_AsyncContextManager(conn))
    handler = NodeLogPersistenceEffect(pool=pool)

    result = await handler.handle(entry)

    assert result.status == "error"
    assert result.error_message is not None
    assert "DB is down" in result.error_message


# ---------------------------------------------------------------------------
# handle_raw synchronous shim
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_handle_raw_returns_skipped_status() -> None:
    entry = _make_entry()
    raw = entry.model_dump(mode="json")
    result = NodeLogPersistenceEffect.handle_raw(raw)

    assert result["entry_id"] == entry.entry_id
    assert result["status"] == "skipped"
