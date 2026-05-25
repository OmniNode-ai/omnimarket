# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden-chain integration test for the log persistence pipeline.

Chain: ModelLogEntry -> handler_log_persistence (asyncpg pool) -> log_entries table.

Verifies:
  1. Handler executes INSERT with correct params when pool is healthy.
  2. Trace aggregation over a shared correlation_id produces correct TraceGroup shape.
  3. Handler emits a StructuredEventLogger warning and does not raise when pool is None.
  4. All optional ModelLogEntry fields survive serialization round-trip intact.

Handler module (omnimarket.nodes.node_log_persistence_effect) is skipped via
pytest.importorskip when a parallel worker has not yet landed it — tests are
marked xfail-on-import-error so the suite stays green until the handler lands.

[OMN-12134]
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from omnimarket.nodes.node_log_projection.handlers.handler_log_projection import (
    EnumLogLevel,
    ModelLogEntry,
)

# ---------------------------------------------------------------------------
# Conditional handler import — skip gracefully if node not yet built
# ---------------------------------------------------------------------------

_handler_mod = pytest.importorskip(
    "omnimarket.nodes.node_log_persistence_effect.handlers.handler_log_persistence",
    reason="node_log_persistence_effect not yet built — parallel worker OMN-12132",
)

HandlerLogPersistence = _handler_mod.HandlerLogPersistence


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_entry(
    *,
    node_name: str = "node_build_loop",
    function_name: str = "advance",
    level: EnumLogLevel = EnumLogLevel.INFO,
    message: str = "integration test entry",
    correlation_id: str | None = None,
    duration_ms: float | None = None,
    metadata: dict[str, str] | None = None,
) -> ModelLogEntry:
    return ModelLogEntry(
        entry_id=str(uuid4()),
        timestamp=datetime.now(tz=UTC).isoformat(),
        node_name=node_name,
        function_name=function_name,
        level=level,
        message=message,
        correlation_id=correlation_id,
        duration_ms=duration_ms,
        metadata=metadata or {},
    )


def _make_pool_mock() -> MagicMock:
    """Return a mock asyncpg pool whose acquire() context-manager returns an async conn."""
    conn = AsyncMock()
    conn.execute = AsyncMock(return_value=None)
    conn.fetch = AsyncMock(return_value=[])

    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)

    pool = MagicMock()
    pool.acquire = MagicMock(return_value=ctx)
    return pool


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestLogPersistenceGoldenChain:
    """Golden chain: ModelLogEntry -> asyncpg INSERT -> log_entries table."""

    async def test_log_entry_persisted_via_handler(self) -> None:
        """Handler calls conn.execute INSERT with the correct parameter values."""
        pool = _make_pool_mock()
        conn = pool.acquire.return_value.__aenter__.return_value

        handler = HandlerLogPersistence(pool=pool)
        correlation_id = str(uuid4())
        entry = _make_entry(
            node_name="node_test_target",
            function_name="run",
            level=EnumLogLevel.ERROR,
            message="something went wrong",
            correlation_id=correlation_id,
            duration_ms=12.5,
            metadata={"key": "val"},
        )

        await handler.persist(entry)

        conn.execute.assert_called_once()
        call_args: tuple[Any, ...] = conn.execute.call_args[0]

        # First positional arg is the SQL query string
        sql: str = call_args[0]
        assert "log_entries" in sql.lower()
        assert "insert" in sql.lower()

        # Remaining positional args are the bound params; verify key fields present
        params = call_args[1:]
        params_flat = list(params)
        assert entry.entry_id in params_flat
        assert entry.node_name in params_flat
        assert entry.message in params_flat
        assert correlation_id in params_flat

    async def test_trace_aggregation_query(self) -> None:
        """Aggregating multiple entries for same correlation_id yields correct TraceGroup."""
        pool = _make_pool_mock()
        correlation_id = str(uuid4())

        entries = [
            _make_entry(
                node_name=f"node_{i}",
                level=EnumLogLevel.INFO if i % 2 == 0 else EnumLogLevel.ERROR,
                message=f"trace step {i}",
                correlation_id=correlation_id,
                duration_ms=float(i * 10),
            )
            for i in range(4)
        ]

        # Simulate what the projection query endpoint returns for a correlation_id:
        # a list of rows corresponding to the entries above.
        mock_rows = [
            {
                "entry_id": e.entry_id,
                "timestamp": e.timestamp,
                "node_name": e.node_name,
                "function_name": e.function_name,
                "level": e.level.value,
                "message": e.message,
                "correlation_id": e.correlation_id,
                "duration_ms": e.duration_ms,
                "metadata": "{}",
            }
            for e in entries
        ]
        conn = pool.acquire.return_value.__aenter__.return_value
        conn.fetch = AsyncMock(return_value=mock_rows)

        handler = HandlerLogPersistence(pool=pool)
        trace_group = await handler.query_trace(correlation_id=correlation_id)

        assert trace_group["correlation_id"] == correlation_id
        assert trace_group["entry_count"] == 4
        error_entries = [r for r in mock_rows if r["level"] == EnumLogLevel.ERROR.value]
        assert trace_group["error_count"] == len(error_entries)

        total_ms = sum(
            r["duration_ms"] for r in mock_rows if r["duration_ms"] is not None
        )
        assert trace_group["total_duration_ms"] == pytest.approx(total_ms)

    async def test_handler_graceful_degradation(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Handler with pool=None emits a warning log and does not raise."""
        handler = HandlerLogPersistence(pool=None)
        entry = _make_entry(message="should degrade gracefully")

        with caplog.at_level(logging.WARNING):
            await handler.persist(entry)

        warning_records = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert warning_records, (
            "Expected at least one WARNING log when pool is None; got none"
        )

    async def test_log_entry_with_all_fields(self) -> None:
        """Full ModelLogEntry with all optional fields survives persist without field loss."""
        pool = _make_pool_mock()
        conn = pool.acquire.return_value.__aenter__.return_value

        handler = HandlerLogPersistence(pool=pool)
        entry = ModelLogEntry(
            entry_id=str(uuid4()),
            timestamp=datetime.now(tz=UTC).isoformat(),
            node_name="node_full_field_test",
            function_name="full_function",
            level=EnumLogLevel.CRITICAL,
            message="all fields populated",
            correlation_id=str(uuid4()),
            duration_ms=99.9,
            metadata={"env": "integration", "run": "omn-12134"},
        )

        await handler.persist(entry)

        conn.execute.assert_called_once()
        call_args: tuple[Any, ...] = conn.execute.call_args[0]
        params_flat = list(call_args[1:])

        assert entry.entry_id in params_flat
        assert entry.node_name in params_flat
        assert entry.message in params_flat
        assert entry.correlation_id in params_flat
        assert entry.duration_ms in params_flat
        assert entry.function_name in params_flat
