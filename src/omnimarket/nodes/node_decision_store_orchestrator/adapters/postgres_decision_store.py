# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Real store adapter: persists/queries decision_store (OMN-14529).

Reuses the EXISTING ``omnibase_infra`` Postgres handlers
(``HandlerWriteDecision`` / ``HandlerQueryDecisions``) instead of
re-implementing persistence — see CLAUDE.md's "reuse the canonical
repository/effect pattern" instruction. Those handlers are ``async def
handle(...)``; ``HandlerDecisionStoreOrchestrator.handle()`` is a synchronous
Protocol boundary (``ProtocolDecisionStoreAdapter``), so this adapter bridges
sync -> async with a one-shot asyncpg pool per call. A one-shot pool is a
correctness requirement, not just simplicity: asyncpg pools are bound to the
event loop that created them, and each ``asyncio.run()`` call opens a new
loop, so a pool cached across calls would break on the second call.

OMN-14529 full-seam-proof correction: driving `onex skill decision_store
record` end-to-end (not just bare handler construction) showed
``HandlerDecisionStoreOrchestrator.handle()`` is invoked from INSIDE
``omnibase_core.runtime.runtime_local.RuntimeLocal``'s already-running
asyncio event loop — ``RuntimeLocal._invoke_handler_method`` calls
``method(initial_payload)`` synchronously from within its own ``async def``
execution path, so ``asyncio.get_running_loop()`` succeeds here even though
``handle()`` itself is a plain sync function. An earlier version of this
bridge treated that as unsupported and raised — that was wrong; it is the
NORMAL production dispatch path, and the earlier bare-handler row-proof
never exercised it (it called `asyncio.run()` once at script top level, so
`_run_async` always saw no running loop). The bridge now runs the coroutine
on a fresh event loop in a dedicated thread and blocks for the result when a
loop is already running — safe because this adapter is always invoked as a
plain (non-awaited) synchronous call from the outer loop, never awaited
directly, so a brief cross-thread block cannot deadlock it.
"""

from __future__ import annotations

import asyncio
import os
import threading
from collections.abc import Coroutine, Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

from omnimarket.nodes.node_decision_store_orchestrator.models.model_decision_store_request import (
    ModelDecisionEntry,
    ModelDecisionQueryFilter,
)

if TYPE_CHECKING:
    from omnibase_core.models.store.model_decision_store_entry import (
        ModelDecisionStoreEntry,
    )

# Canonical DSN env var for the omnibase_infra Postgres database that holds
# decision_store (see docker/catalog/services/*.yaml and ~/.omnibase/.env —
# OMNIBASE_INFRA_DB_URL is the same variable production consumers use).
_DSN_ENV_VAR = "OMNIBASE_INFRA_DB_URL"

# decision_store's source enum (ModelPayloadWriteDecision.source /
# ModelDecisionStoreEntry.source) is not part of the orchestrator's own
# ModelDecisionEntry — the orchestrator's request shape carries no caller
# identity or provenance fields. "manual" is the most honest default for
# decisions recorded through the CLI dispatch path.
_DEFAULT_SOURCE = "manual"


def _require_dsn() -> str:
    dsn = os.environ.get(_DSN_ENV_VAR)
    if not dsn:
        raise RuntimeError(
            f"{_DSN_ENV_VAR} is not set — required to persist/query "
            "decision_store. Fail-fast per CLAUDE.md rule 8: no silent "
            "default DSN."
        )
    return dsn


def _run_async[T](coro: Coroutine[Any, Any, T]) -> T:
    """Bridge a coroutine into this synchronous adapter method.

    No running loop (e.g. a script calling the handler directly): run the
    coroutine on a fresh loop via ``asyncio.run()`` in the current thread.

    A loop IS already running (the real production path — see module
    docstring): ``asyncio.run()`` cannot nest inside a running loop, so run
    the coroutine on a fresh loop in a dedicated thread and block for the
    result.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    result: list[T] = []
    error: list[BaseException] = []

    def _run_in_new_loop() -> None:
        try:
            result.append(asyncio.run(coro))
        except BaseException as exc:
            error.append(exc)

    thread = threading.Thread(target=_run_in_new_loop)
    thread.start()
    thread.join()
    if error:
        raise error[0]
    return result[0]


class PostgresDecisionStore:
    """Real ``ProtocolDecisionStoreAdapter`` implementation.

    Args:
        dsn: Postgres DSN override. Defaults to the ``OMNIBASE_INFRA_DB_URL``
            environment variable, resolved lazily (not at construction time)
            so this class can be the module-level default on
            ``HandlerDecisionStoreOrchestrator.__init__`` without requiring
            the DSN to be set merely to import the module.
    """

    def __init__(self, dsn: str | None = None) -> None:
        self._dsn = dsn

    # -- ProtocolDecisionStoreAdapter -----------------------------------

    def persist_decision(
        self,
        entry: ModelDecisionEntry,
        conflicts: tuple[object, ...],
    ) -> str:
        return _run_async(self._persist_decision_async(entry))

    def query_decisions(
        self, query_filter: ModelDecisionQueryFilter | None
    ) -> Mapping[str, Any]:
        return _run_async(self._query_decisions_async(query_filter))

    # -- non-Protocol: preserves the real decision_id for conflict identity
    # (ModelDecisionEntry has no id field — see StructuralConflictCheck)

    def query_active_decisions_raw(
        self, *, domain: str, layer: str, limit: int = 100
    ) -> tuple[ModelDecisionStoreEntry, ...]:
        return _run_async(
            self._query_raw_async(domain=domain, layer=layer, limit=limit)
        )

    # -- async implementations -------------------------------------------

    async def _persist_decision_async(self, entry: ModelDecisionEntry) -> str:
        import asyncpg
        from omnibase_infra.nodes.node_decision_store_effect.handlers.handler_write_decision import (
            HandlerWriteDecision,
        )
        from omnibase_infra.nodes.node_decision_store_effect.models.model_payload_write_decision import (
            ModelPayloadWriteDecision,
        )

        dsn = self._dsn or _require_dsn()
        decision_id = uuid4()
        correlation_id = uuid4()
        payload = ModelPayloadWriteDecision(
            correlation_id=correlation_id,
            decision_id=decision_id,
            title=entry.summary,
            decision_type=entry.decision_type.value,
            scope_domain=entry.domain,
            scope_services=list(entry.services),
            scope_layer=entry.layer.value,
            rationale=entry.rationale,
            source=_DEFAULT_SOURCE,
            created_at=datetime.now(UTC),
            created_by=os.environ.get("USER", "onex-skill-decision_store"),
        )
        pool = await asyncpg.create_pool(dsn=dsn, min_size=1, max_size=2)
        try:
            handler = HandlerWriteDecision(pool)
            result = await handler.handle(payload, correlation_id)
            if not result.success:
                raise RuntimeError(
                    f"decision_store write failed: {result.error} ({result.error_code})"
                )
        finally:
            await pool.close()
        return str(decision_id)

    async def _query_raw_async(
        self, *, domain: str, layer: str, limit: int
    ) -> tuple[ModelDecisionStoreEntry, ...]:
        import asyncpg
        from omnibase_infra.nodes.node_decision_store_query_compute.handlers.handler_query_decisions import (
            HandlerQueryDecisions,
        )
        from omnibase_infra.nodes.node_decision_store_query_compute.models.model_payload_query_decisions import (
            ModelPayloadQueryDecisions,
        )

        dsn = self._dsn or _require_dsn()
        payload = ModelPayloadQueryDecisions(domain=domain, layer=layer, limit=limit)
        pool = await asyncpg.create_pool(dsn=dsn, min_size=1, max_size=2)
        try:
            handler = HandlerQueryDecisions(pool)
            result = await handler.handle(payload)
        finally:
            await pool.close()
        # ModelResultDecisionList.decisions is typed tuple[object, ...] pending
        # OMN-2763's ModelDecisionStoreEntry merge (see model_result_decision_list.py);
        # HandlerQueryDecisions._row_to_entry always constructs the concrete type.
        return cast("tuple[ModelDecisionStoreEntry, ...]", tuple(result.decisions))

    async def _query_decisions_async(
        self, query_filter: ModelDecisionQueryFilter | None
    ) -> Mapping[str, Any]:
        import asyncpg
        from omnibase_infra.nodes.node_decision_store_query_compute.handlers.handler_query_decisions import (
            HandlerQueryDecisions,
        )
        from omnibase_infra.nodes.node_decision_store_query_compute.models.model_payload_query_decisions import (
            ModelPayloadQueryDecisions,
        )

        dsn = self._dsn or _require_dsn()
        payload = ModelPayloadQueryDecisions(
            domain=query_filter.domain if query_filter else None,
            layer=(
                query_filter.layer.value
                if query_filter and query_filter.layer
                else None
            ),
            decision_type=(
                [query_filter.decision_type.value]
                if query_filter and query_filter.decision_type
                else None
            ),
            cursor=query_filter.cursor if query_filter else None,
            limit=query_filter.limit if query_filter else 20,
        )
        pool = await asyncpg.create_pool(dsn=dsn, min_size=1, max_size=2)
        try:
            handler = HandlerQueryDecisions(pool)
            result = await handler.handle(payload)
        finally:
            await pool.close()
        entries = tuple(_to_orchestrator_entry_dict(row) for row in result.decisions)
        return {"entries": entries, "next_cursor": result.next_cursor}


def _to_orchestrator_entry_dict(row: Any) -> dict[str, Any]:
    """Map a queried ``ModelDecisionStoreEntry`` back onto the
    orchestrator's minimal ``ModelDecisionEntry`` shape.

    ModelDecisionStoreEntry (omnibase_core) has no summary/title field — see
    the NOTE in handler_query_decisions.py — so rationale fills both roles
    on the read side. decision_type case also differs: omnibase_core's
    EnumDecisionType is lowercase, the orchestrator's is uppercase; both
    vocabularies share the 5 decision_store members 1:1 by case only.
    """
    decision_type_raw = row.decision_type
    decision_type = (
        decision_type_raw.value
        if hasattr(decision_type_raw, "value")
        else str(decision_type_raw)
    )
    return {
        "decision_type": decision_type.upper(),
        "domain": row.scope_domain,
        "layer": row.scope_layer,
        "services": tuple(row.scope_services),
        "summary": row.rationale[:200],
        "rationale": row.rationale,
    }


__all__ = ["PostgresDecisionStore"]
