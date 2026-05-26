# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""NodeLogPersistenceEffect — persists log events to Postgres.

Subscribes to onex.evt.platform.log-entry.v1 events and INSERTs each into
the log_entries table. Idempotent on entry_id via ON CONFLICT DO NOTHING.

If the asyncpg pool is None (no DB configured), the handler logs a warning
and skips the write rather than raising — graceful degradation for
environments without Postgres.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.contract_topics import (
    contract_publish_topics,
    contract_subscribe_topics,
)
from omnimarket.nodes.node_log_projection.handlers.handler_log_projection import (
    ModelLogEntry,
)

if TYPE_CHECKING:
    import asyncpg

logger = logging.getLogger(__name__)

_CONTRACT_PATH = Path(__file__).parent.parent / "contract.yaml"
_SUBSCRIBE_TOPICS = contract_subscribe_topics(_CONTRACT_PATH)
_PUBLISH_TOPICS = contract_publish_topics(_CONTRACT_PATH)

_DEFAULT_PG_DSN = os.environ.get("ONEX_PG_DSN", "")  # contract-config-ok: config  # fmt: skip


class ModelLogPersistenceResult(BaseModel):
    """Result of a single log entry persistence attempt."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    result_id: str = Field(default_factory=lambda: str(uuid4()))
    entry_id: str = Field(..., description="entry_id of the persisted log entry.")
    status: Literal["written", "skipped", "idempotent", "error"] = Field(...)
    error_message: str | None = Field(default=None)


class NodeLogPersistenceEffect:
    """EFFECT node: persists log events from Kafka to Postgres.

    Accepts an optional asyncpg pool for dependency injection (enables unit
    testing without a real database). When pool is None and no DSN is
    configured, operates in no-op mode with a logged warning.
    """

    handler_type: Literal["node_handler"] = "node_handler"
    handler_category: Literal["effect"] = "effect"

    def __init__(
        self,
        pool: asyncpg.Pool | None = None,
        pg_dsn: str = _DEFAULT_PG_DSN,
    ) -> None:
        self._injected_pool = pool
        self._pg_dsn = pg_dsn
        self._pool: asyncpg.Pool | None = pool

    async def _get_pool(self) -> asyncpg.Pool | None:
        """Return pool, lazily creating it from DSN if not injected."""
        if self._pool is not None:
            return self._pool
        if not self._pg_dsn:
            return None
        import asyncpg as _asyncpg

        self._pool = await _asyncpg.create_pool(
            dsn=self._pg_dsn,
            min_size=1,
            max_size=5,
            command_timeout=10,
        )
        return self._pool

    async def handle(self, entry: ModelLogEntry) -> ModelLogPersistenceResult:
        """Persist a single log entry to Postgres.

        Args:
            entry: The structured log event to persist.

        Returns:
            ModelLogPersistenceResult describing the outcome.
        """
        pool = await self._get_pool()

        if pool is None:
            logger.warning(
                "NodeLogPersistenceEffect: no DB pool available, skipping entry_id=%s",
                entry.entry_id,
            )
            return ModelLogPersistenceResult(
                entry_id=entry.entry_id,
                status="skipped",
            )

        try:
            inserted = await self._insert(pool, entry)
            status: Literal["written", "idempotent"] = (
                "written" if inserted else "idempotent"
            )
            logger.debug(
                "NodeLogPersistenceEffect: entry_id=%s status=%s",
                entry.entry_id,
                status,
            )
            return ModelLogPersistenceResult(entry_id=entry.entry_id, status=status)
        except Exception as exc:
            logger.error(
                "NodeLogPersistenceEffect: failed to persist entry_id=%s: %s",
                entry.entry_id,
                exc,
            )
            return ModelLogPersistenceResult(
                entry_id=entry.entry_id,
                status="error",
                error_message=str(exc),
            )

    async def _insert(self, pool: asyncpg.Pool, entry: ModelLogEntry) -> bool:
        """INSERT entry into log_entries. Returns True if a row was inserted."""
        metadata_json = json.dumps(entry.metadata)
        async with pool.acquire() as conn:
            inserted_id = await conn.fetchval(
                """
                INSERT INTO log_entries (
                    entry_id,
                    timestamp,
                    node_name,
                    function_name,
                    level,
                    message,
                    correlation_id,
                    duration_ms,
                    metadata
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                ON CONFLICT (entry_id) DO NOTHING
                RETURNING entry_id
                """,
                entry.entry_id,
                entry.timestamp,
                entry.node_name,
                entry.function_name,
                entry.level.value,
                entry.message,
                entry.correlation_id,
                entry.duration_ms,
                metadata_json,
            )
        return inserted_id is not None

    @staticmethod
    def handle_raw(input_data: dict[str, Any]) -> dict[str, Any]:
        """Synchronous dict-in / dict-out shim for runtime protocol compatibility."""
        entry = ModelLogEntry(**input_data)
        return {
            "entry_id": entry.entry_id,
            "status": "skipped",
            "error_message": "use async handle() for live persistence",
        }


__all__: list[str] = ["ModelLogPersistenceResult", "NodeLogPersistenceEffect"]
