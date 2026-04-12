# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# Copyright (c) 2025 OmniNode Team
"""Handler for the NavigationHistoryReducer node.

Implements persistence of completed navigation sessions:

- **Success paths**: Written to both PostgreSQL (``navigation_sessions`` table)
  and Qdrant (``navigation_paths`` collection) for retrieval-augmented navigation.
- **Failure paths**: Written to PostgreSQL only. Failed paths are never written
  to Qdrant — they must not appear as positive retrieval examples.

Design decisions:
- All writes are fire-and-forget from the navigation session's perspective.
  Write failures are logged but never propagated to callers.
- Idempotent on ``session_id``: duplicate writes are silently no-ops.
- Embeddings are obtained from the Qwen3-Embedding-8B model at
  ``LLM_EMBEDDING_URL`` (configured via environment variable).
- PostgreSQL uses ``asyncpg`` for async connection management.
- Qdrant uses the ``qdrant-client`` async API.

.. versionadded:: 0.4.0
    Initial implementation for OMN-2584 Navigation History Storage.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from typing import TYPE_CHECKING
from uuid import UUID

import structlog
from qdrant_client import AsyncQdrantClient
from qdrant_client.http import models as qdrant_models

if TYPE_CHECKING:
    import asyncpg

from omnimarket.nodes.node_navigation_history_reducer.models.model_navigation_history_request import (
    ModelNavigationHistoryRequest,
)
from omnimarket.nodes.node_navigation_history_reducer.models.model_navigation_history_response import (
    ModelNavigationHistoryResponse,
)
from omnimarket.nodes.node_navigation_history_reducer.models.model_navigation_session import (
    ModelNavigationOutcomeFailure,
    ModelNavigationSession,
)

logger = logging.getLogger(__name__)
structured_logger = structlog.get_logger(__name__)

__all__ = ["HandlerNavigationHistoryReducer", "HandlerNavigationHistoryWriter"]

# ---------------------------------------------------------------------------
# Constants (env-var driven; no hardcoded internal IPs)
# ---------------------------------------------------------------------------

_DEFAULT_PG_DSN = os.environ.get("OMNIMEMORY_PG_DSN", "")
_DEFAULT_QDRANT_HOST = os.environ.get("QDRANT_HOST", "")
_DEFAULT_QDRANT_PORT = int(os.environ.get("QDRANT_PORT", "6333"))
_DEFAULT_EMBEDDING_URL = os.environ.get("LLM_EMBEDDING_URL", "")
_DEFAULT_EMBEDDING_MODEL = "Qwen3-Embedding-8B"
_QDRANT_COLLECTION = "navigation_paths"

# Qdrant vector dimension for Qwen3-Embedding-8B (4096-dim output)
_EMBEDDING_DIM = 4096


class HandlerNavigationHistoryWriter:
    """Persistence writer for completed navigation sessions.

    ``NodeNavigationHistoryReducer`` to durably store navigation sessions
    after they complete. All I/O is async; the public ``record()`` method
    is intended to be called fire-and-forget via ``asyncio.create_task()``.

    Attributes:
        _pg_dsn: PostgreSQL DSN for the ``navigation_sessions`` table.
        _qdrant_host: Qdrant server hostname.
        _qdrant_port: Qdrant server port.
        _embedding_url: URL for the embedding model endpoint.
        _embedding_model: Model name to request from the embedding endpoint.
        _pg_pool: Shared asyncpg connection pool (created on first use).
        _qdrant_client: Shared Qdrant async client (created on first use).
    """

    def __init__(
        self,
        pg_dsn: str = _DEFAULT_PG_DSN,
        qdrant_host: str = _DEFAULT_QDRANT_HOST,
        qdrant_port: int = _DEFAULT_QDRANT_PORT,
        embedding_url: str = _DEFAULT_EMBEDDING_URL,
        embedding_model: str = _DEFAULT_EMBEDDING_MODEL,
    ) -> None:
        """Initialize the writer with connection parameters.

        Args:
            pg_dsn: PostgreSQL DSN string for the omnimemory database.
            qdrant_host: Hostname of the Qdrant server.
            qdrant_port: Port of the Qdrant server.
            embedding_url: Full URL for the embedding API endpoint.
            embedding_model: Model identifier to pass to the embedding endpoint.
        """
        self._pg_dsn = pg_dsn
        self._qdrant_host = qdrant_host
        self._qdrant_port = qdrant_port
        self._embedding_url = embedding_url
        self._embedding_model = embedding_model

        self._pg_pool: asyncpg.Pool | None = None
        self._qdrant_client: AsyncQdrantClient | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def record(
        self, session: ModelNavigationSession
    ) -> ModelNavigationHistoryResponse:
        """Record a completed navigation session to persistent storage.

        Routing:
        - ``Success``: write to PostgreSQL AND Qdrant.
        - ``Failure``: write to PostgreSQL ONLY.

        Write failures are logged with structured context but never raised —
        the navigation session must not be impacted by storage errors.
        Duplicate ``session_id`` writes are silently no-ops (idempotent).

        Args:
            session: The completed navigation session to persist.

        Returns:
            A response summarising what was written. This return value is
            informational; callers using fire-and-forget do not observe it.
        """
        log = structured_logger.bind(
            session_id=str(session.session_id),
            outcome=session.final_outcome.tag,
            step_count=session.step_count,
            graph_fingerprint=session.graph_fingerprint,
        )

        postgres_written = False
        qdrant_written = False
        idempotent_skip = False

        try:
            pool = await self._get_pg_pool()
            pg_result = await self._write_postgres(pool, session)
            if pg_result == "idempotent":
                log.info("navigation_history_idempotent_skip")
                return ModelNavigationHistoryResponse(
                    session_id=session.session_id,
                    status="skipped",
                    idempotent_skip=True,
                )
            postgres_written = True
            log.info("navigation_history_postgres_written")
        except Exception as exc:
            log.error(
                "navigation_history_postgres_write_failed",
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return ModelNavigationHistoryResponse(
                session_id=session.session_id,
                status="error",
                postgres_written=False,
                error_message=f"PostgreSQL write failed: {exc}",
            )

        # Only write to Qdrant for successful navigation sessions
        if session.is_successful:
            try:
                client = await self._get_qdrant_client()
                await self._write_qdrant(client, session)
                qdrant_written = True
                log.info("navigation_history_qdrant_written")
            except Exception as exc:
                # Qdrant write failure is non-fatal — PostgreSQL record stands
                log.error(
                    "navigation_history_qdrant_write_failed",
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
                # Return partial success — PostgreSQL is the source of truth
                return ModelNavigationHistoryResponse(
                    session_id=session.session_id,
                    status="error",
                    postgres_written=postgres_written,
                    qdrant_written=False,
                    error_message=f"Qdrant write failed (PostgreSQL OK): {exc}",
                )

        return ModelNavigationHistoryResponse(
            session_id=session.session_id,
            status="success",
            postgres_written=postgres_written,
            qdrant_written=qdrant_written,
            idempotent_skip=idempotent_skip,
        )

    async def close(self) -> None:
        """Release all pooled connections.

        Call this during application shutdown to cleanly release resources.
        """
        if self._pg_pool is not None:
            await self._pg_pool.close()
            self._pg_pool = None
        if self._qdrant_client is not None:
            await self._qdrant_client.close()
            self._qdrant_client = None

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    async def _get_pg_pool(self) -> asyncpg.Pool:
        """Return the shared asyncpg connection pool, creating it if needed."""
        import asyncpg as _asyncpg

        if self._pg_pool is None:
            self._pg_pool = await _asyncpg.create_pool(
                dsn=self._pg_dsn,
                min_size=1,
                max_size=5,
                command_timeout=30,
            )
        assert self._pg_pool is not None
        return self._pg_pool

    async def _get_qdrant_client(self) -> AsyncQdrantClient:
        """Return the shared Qdrant async client, creating it if needed."""
        if self._qdrant_client is None:
            self._qdrant_client = AsyncQdrantClient(
                host=self._qdrant_host,
                port=self._qdrant_port,
                timeout=30,
            )
        return self._qdrant_client

    # ------------------------------------------------------------------
    # PostgreSQL write
    # ------------------------------------------------------------------

    async def _write_postgres(
        self,
        pool: asyncpg.Pool,
        session: ModelNavigationSession,
    ) -> str:
        """Write navigation session to the ``navigation_sessions`` table.

        Args:
            pool: asyncpg connection pool.
            session: The session to persist.

        Returns:
            ``"written"`` if a new row was inserted, ``"idempotent"`` if the
            session_id already existed.

        Raises:
            asyncpg.PostgresError: On any unexpected database error.
        """
        outcome_tag = session.final_outcome.tag
        failure_reason: str | None = None
        if isinstance(session.final_outcome, ModelNavigationOutcomeFailure):
            failure_reason = session.final_outcome.reason

        goal_hash = _hash_text(session.goal_condition)
        steps_json = json.dumps(
            [
                {
                    "step_index": step.step_index,
                    "from_state_id": step.from_state_id,
                    "to_state_id": step.to_state_id,
                    "action": step.action,
                    "executed_at": step.executed_at.isoformat(),
                }
                for step in session.executed_steps
            ]
        )

        async with pool.acquire() as conn:
            # Single-statement insert with conflict guard — eliminates the
            # TOCTOU race between a SELECT existence check and the INSERT.
            # Returns the inserted session_id on success, or nothing if the
            # row already exists (concurrent duplicate write).
            inserted = await conn.fetchval(
                """
                INSERT INTO navigation_sessions (
                    session_id,
                    goal_hash,
                    goal_condition,
                    start_state_id,
                    end_state_id,
                    step_count,
                    outcome,
                    failure_reason,
                    graph_fingerprint,
                    steps_json,
                    created_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                ON CONFLICT (session_id) DO NOTHING
                RETURNING session_id
                """,
                str(session.session_id),
                goal_hash,
                session.goal_condition,
                session.start_state_id,
                session.end_state_id,
                session.step_count,
                outcome_tag,
                failure_reason,
                session.graph_fingerprint,
                steps_json,
                session.created_at,
            )

        if inserted is None:
            return "idempotent"
        return "written"

    # ------------------------------------------------------------------
    # Qdrant write
    # ------------------------------------------------------------------

    async def _write_qdrant(
        self,
        client: AsyncQdrantClient,
        session: ModelNavigationSession,
    ) -> None:
        """Write a successful navigation session to the Qdrant collection.

        Embeds the goal condition and start state using the configured
        embedding model, then upserts a point into ``navigation_paths``.

        Only called for successful sessions — this invariant is enforced by
        the caller (``record()``).

        Args:
            client: Async Qdrant client.
            session: A successful navigation session.

        Raises:
            Exception: On embedding API or Qdrant upsert failures.
        """
        # Embed goal and start state
        goal_vector = await self._embed_text(session.goal_condition)
        start_vector = await self._embed_text(session.start_state_id)

        # Payload stored alongside the vectors — typed graph artifacts only
        payload: dict[str, str | int | list[dict[str, str | int]]] = {
            "session_id": str(session.session_id),
            "goal_condition": session.goal_condition,
            "goal_hash": _hash_text(session.goal_condition),
            "start_state_id": session.start_state_id,
            "end_state_id": session.end_state_id,
            "step_count": session.step_count,
            "outcome": session.final_outcome.tag,
            "graph_fingerprint": session.graph_fingerprint,
            "created_at": session.created_at.isoformat(),
            "steps": [
                {
                    "step_index": step.step_index,
                    "from_state_id": step.from_state_id,
                    "to_state_id": step.to_state_id,
                    "action": step.action,
                    "executed_at": step.executed_at.isoformat(),
                }
                for step in session.executed_steps
            ],
        }

        # Use a deterministic Qdrant point ID derived from session_id
        # so upserts are idempotent at the Qdrant level as well.
        point_id = _uuid_to_qdrant_id(session.session_id)

        point = qdrant_models.PointStruct(
            id=point_id,
            vector={
                "goal": goal_vector,
                "start_state": start_vector,
            },
            payload=payload,
        )

        await client.upsert(
            collection_name=_QDRANT_COLLECTION,
            points=[point],
            wait=True,
        )

    # ------------------------------------------------------------------
    # Embedding helper
    # ------------------------------------------------------------------

    async def _embed_text(self, text: str) -> list[float]:
        """Obtain an embedding vector from the configured embedding endpoint.

        Args:
            text: The text to embed.

        Returns:
            A list of floats representing the embedding vector.

        Raises:
            Exception: If the embedding service returns an error or the
                response does not contain a valid embedding.
        """
        import httpx as _httpx  # omnimemory-http-exempt: Phase 2 handler; local import to satisfy static scan; Phase 3 will inject via adapter (OMN-2584)

        async with _httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                self._embedding_url,
                json={
                    "model": self._embedding_model,
                    "input": text,
                },
                headers={"Content-Type": "application/json"},
            )
            response.raise_for_status()
            data = response.json()

        try:
            vector: list[float] = data["data"][0]["embedding"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError(
                f"Unexpected embedding response structure: {data!r}"
            ) from exc

        return vector


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


def _hash_text(text: str) -> str:
    """Return a stable SHA-256 hex digest of the given text.

    Used for the ``goal_hash`` column — a compact, indexed representation of
    the goal condition that allows fast lookup of previously-seen goals.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _uuid_to_qdrant_id(session_id: UUID) -> str:
    """Convert a UUID to a Qdrant-compatible string point ID."""
    return str(session_id)


# ---------------------------------------------------------------------------
# Handler class (ONEX Reducer node handler)
# ---------------------------------------------------------------------------


class HandlerNavigationHistoryReducer:
    """ONEX handler for the ``navigation_history_reducer`` node.

    Wraps ``HandlerNavigationHistoryWriter`` with the fire-and-forget execution
    pattern required by the ONEX Reducer contract. The public ``execute()``
    method schedules the write as a background task and returns immediately,
    ensuring that navigation sessions are never blocked by storage latency or
    failures.

    Attributes:
        _writer: The underlying ``HandlerNavigationHistoryWriter`` instance.
        _initialized: Whether ``initialize()`` has been called.

    Example::

        handler = HandlerNavigationHistoryReducer()
        await handler.initialize()

        # Fire-and-forget — navigation session continues immediately
        asyncio.create_task(handler.execute(request))

        await handler.shutdown()
    """

    def __init__(
        self,
        writer: HandlerNavigationHistoryWriter | None = None,
        pg_dsn: str = _DEFAULT_PG_DSN,
        qdrant_host: str = _DEFAULT_QDRANT_HOST,
        qdrant_port: int = _DEFAULT_QDRANT_PORT,
        embedding_url: str = _DEFAULT_EMBEDDING_URL,
        embedding_model: str = _DEFAULT_EMBEDDING_MODEL,
    ) -> None:
        """Initialize the handler.

        Args:
            writer: Optional pre-constructed ``HandlerNavigationHistoryWriter``.
                If not provided, one is created from the remaining kwargs.
            pg_dsn: PostgreSQL DSN (used only if ``writer`` is None).
            qdrant_host: Qdrant hostname (used only if ``writer`` is None).
            qdrant_port: Qdrant port (used only if ``writer`` is None).
            embedding_url: Embedding endpoint URL (used only if ``writer`` is None).
            embedding_model: Embedding model name (used only if ``writer`` is None).
        """
        if writer is not None:
            self._writer = writer
        else:
            self._writer = HandlerNavigationHistoryWriter(
                pg_dsn=pg_dsn,
                qdrant_host=qdrant_host,
                qdrant_port=qdrant_port,
                embedding_url=embedding_url,
                embedding_model=embedding_model,
            )
        self._initialized = False

    @property
    def is_initialized(self) -> bool:
        """Return True if ``initialize()`` has been called."""
        return self._initialized

    async def initialize(self) -> None:
        """Prepare the handler for use.

        This is a lifecycle hook required by ONEX handler conventions.
        The underlying writer lazily initialises connections on first use,
        so no I/O happens here.
        """
        if self._initialized:
            return
        self._initialized = True
        logger.info("HandlerNavigationHistoryReducer initialized")

    async def shutdown(self) -> None:
        """Release all pooled connections.

        Must be called during application shutdown to prevent connection leaks.
        """
        await self._writer.close()
        self._initialized = False
        logger.info("HandlerNavigationHistoryReducer shutdown")

    async def execute(
        self, request: ModelNavigationHistoryRequest
    ) -> ModelNavigationHistoryResponse:
        """Execute the reduction: record the navigation session.

        This method is safe to call without awaiting (fire-and-forget pattern)
        via ``asyncio.create_task(handler.execute(request))``. Write errors are
        logged but not re-raised.

        Args:
            request: The record request containing the navigation session.

        Returns:
            A response describing what was written. Not observed by callers
            using fire-and-forget.
        """
        if not self._initialized:
            return ModelNavigationHistoryResponse(
                session_id=request.session.session_id,
                status="error",
                error_message="Handler not initialized. Call initialize() first.",
            )

        try:
            return await self._writer.record(request.session)
        except Exception as exc:
            # Belt-and-suspenders: writer.record() is already exception-safe,
            # but we guard here as well so the task never raises unhandled.
            structured_logger.error(
                "navigation_history_reducer_unhandled_error",
                session_id=str(request.session.session_id),
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return ModelNavigationHistoryResponse(
                session_id=request.session.session_id,
                status="error",
                error_message=f"Unhandled error: {exc}",
            )
