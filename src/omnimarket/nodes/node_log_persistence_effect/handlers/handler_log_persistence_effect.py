# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""NodeLogPersistenceEffect — persists log events to Postgres.

Subscribes to structured log and delegation terminal events and materializes
log_entries through the runtime database adapter. Replay-safe on entry_id.

The bus handler uses the database supplied by contract-driven runtime wiring.
The legacy async persist API remains available for direct callers.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID, uuid4

import yaml
from omnibase_core.models.logging.model_structured_log_entry import (
    ModelStructuredLogEntry,
)
from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.delegation.wire.model_delegate_skill_response import (
    ModelDelegateSkillCompleted,
    ModelDelegateSkillFailed,
)
from omnimarket.nodes.contract_topics import (
    contract_publish_topics,
    contract_subscribe_topics,
)
from omnimarket.nodes.node_log_projection.handlers.handler_log_projection import (
    ModelLogEntry,
)
from omnimarket.projection.envelope import envelope_event_timestamp
from omnimarket.projection.handler_shim import split_projection_input

if TYPE_CHECKING:
    import asyncpg

logger = logging.getLogger(__name__)

_CONTRACT_PATH = Path(__file__).parent.parent / "contract.yaml"
_SUBSCRIBE_TOPICS = contract_subscribe_topics(_CONTRACT_PATH)
_PUBLISH_TOPICS = contract_publish_topics(_CONTRACT_PATH)
_SUPPORTED_EVENT_MODELS: dict[
    str,
    type[ModelStructuredLogEntry]
    | type[ModelDelegateSkillCompleted]
    | type[ModelDelegateSkillFailed],
] = {
    f"{cls.__module__}.{cls.__qualname__}": cls
    for cls in (
        ModelStructuredLogEntry,
        ModelDelegateSkillCompleted,
        ModelDelegateSkillFailed,
    )
}


def _load_handler_event_models(contract_path: Path) -> dict[str, str]:
    """Map each contract-routed topic to its declared event_model path."""
    contract = yaml.safe_load(contract_path.read_text())
    routes: dict[str, str] = {}
    for route in contract["handler_routing"]["handlers"]:
        if route["event_model"] not in _SUPPORTED_EVENT_MODELS:
            raise ValueError(
                "unsupported log persistence contract event_model: "
                f"{route['event_model']!r}"
            )
        routes[route["topic"]] = route["event_model"]
    return routes


_HANDLER_EVENT_MODELS = _load_handler_event_models(_CONTRACT_PATH)

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

    Bus dispatch requires the contract-resolved database adapter. Direct
    legacy persist() calls may supply an asyncpg pool or DSN separately.
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

    def handle(self, request: object) -> dict[str, object]:
        """Persist bus activity through the contract-resolved projection database.

        Synchronous by design: callers must not await it. The runtime db_io
        projection dispatch calls this method without awaiting, and
        DatabaseAdapter.upsert is synchronous; the async direct-write API is
        persist(). This dispatch shape avoids the legacy async path's optional
        ONEX_PG_DSN and silent skipped writes.
        Delegation terminals produce execution logs, never copies of model text.

        ``request`` is the runtime-injected payload mapping (with ``_db``,
        ``_topic`` and envelope metadata), the shape the db_io projection
        dispatch arm sends; see node_hook_event_capture for the same contract.
        """
        if not isinstance(request, Mapping):
            raise TypeError(
                "NodeLogPersistenceEffect.handle() expects the runtime-injected "
                f"payload mapping (with _db/_topic), got {type(request).__name__}"
            )
        db, payload, meta = split_projection_input(dict(request))
        topic = meta.get("_topic")
        if not isinstance(topic, str) or topic not in _HANDLER_EVENT_MODELS:
            raise ValueError(
                f"log persistence topic is not routed by contract: {topic!r}"
            )
        event_cls = _SUPPORTED_EVENT_MODELS[_HANDLER_EVENT_MODELS[topic]]
        event = event_cls.model_validate(payload)
        if isinstance(event, (ModelDelegateSkillCompleted, ModelDelegateSkillFailed)):
            terminal = event
            # Source identity and event time must survive replay unchanged.
            if "_envelope_id" not in meta:
                raise ValueError("delegation execution log requires source envelope id")
            entry_id = UUID(str(meta["_envelope_id"]))
            timestamp = envelope_event_timestamp(meta)
            if timestamp is None:
                raise ValueError(
                    "delegation execution log requires source envelope timestamp"
                )
            row: dict[str, object] = {
                "entry_id": entry_id,
                "timestamp": timestamp,
                "node_name": "node_delegate_skill_orchestrator",
                "function_name": "delegate_skill",
                "level": "info" if terminal.status == "completed" else "error",
                "message": f"Delegation {terminal.status}",
                "correlation_id": str(terminal.correlation_id),
                "duration_ms": terminal.execution_duration_ms,
                "metadata": {
                    "task_type": terminal.task_type,
                    "model_name": terminal.model_name,
                },
            }
        else:
            entry = event
            duration_ms = None
            if "duration_ms" in entry.metadata:
                try:
                    duration_ms = float(entry.metadata["duration_ms"])
                except (TypeError, ValueError) as exc:
                    raise ValueError(
                        "structured log metadata duration_ms must be convertible to float"
                    ) from exc
            row = {
                "entry_id": entry.entry_id,
                "timestamp": entry.timestamp,
                "node_name": entry.source_system,
                "function_name": entry.operation,
                "level": entry.level.value.lower(),
                "message": entry.message,
                "correlation_id": None
                if entry.correlation_id is None
                else str(entry.correlation_id),
                "duration_ms": duration_ms,
                "metadata": entry.metadata,
            }
        written = db.upsert("log_entries", "entry_id", row)
        return {
            "entry_id": str(row["entry_id"]),
            "status": "written" if written else "idempotent",
            "rows_upserted": int(written),
        }

    async def persist(self, entry: ModelLogEntry) -> ModelLogPersistenceResult:
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
                INSERT INTO omninode_internal.log_entries (
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
            "error_message": "use persist() for legacy direct persistence",
        }


__all__: list[str] = ["ModelLogPersistenceResult", "NodeLogPersistenceEffect"]
