# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Postgres-backed ``ProtocolCodeEntityRepository`` implementation (OMN-15230).

This is the first production implementation of the protocol. Before it,
``node_code_embedding_effect`` and ``node_code_enrichment_effect`` were
boot-resolvable (OMN-15228 / OMN-15229) but *dispatch-non-functional*: the
container had no provider, so ``get_service(ProtocolCodeEntityRepository)``
raised on the first real dispatch of either node.

Store
-----
The ``code_entities`` table in the ``omniintelligence`` database — the
AST-extraction store written by the code-entity extraction pipeline. Column
vocabulary is taken from that store's DDL (``qualified_name``/``source_repo``
upsert key, ``last_extracted_at`` / ``last_enriched_at`` / ``last_embedded_at``
freshness stamps) and cross-checked against what the two handlers actually read:
``build_embedding_text`` consumes ``entity_name`` / ``signature`` / ``docstring``
/ ``llm_description``; ``_upsert_point`` consumes ``id`` / ``entity_type`` /
``qualified_name`` / ``source_repo`` / ``source_path`` / ``classification``;
``_enrich_single_entity`` consumes ``entity_name`` / ``bases`` / ``methods`` /
``docstring``.

Connection
----------
DSN comes from ``OMNIINTELLIGENCE_DB_URL`` — the sanctioned per-service DB URL
for this database (``onex_change_control/env_contract.yaml`` ``allowed_required``)
and the same key the runtime's contract ``database: omniintelligence`` binding
maps to. There is **no default DSN**: an unset env var raises at first use rather
than silently pointing at some other database (CLAUDE.md rule 8).

The pool adapter is connected lazily, on first query — not in ``__init__`` — so
the repository can be constructed and registered in the container at kernel boot
without opening a connection, and a mis-set DSN surfaces at the effect boundary
with a message naming the env var. Raw pool construction stays behind
``AsyncpgAdapter``; this module owns repository queries, not DB-driver setup.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any

from omnimarket.adapters.asyncpg_adapter import AsyncpgAdapter

if TYPE_CHECKING:
    import asyncpg

logger = logging.getLogger(__name__)

#: Env var holding the DSN of the database that owns ``code_entities``.
CODE_ENTITY_DB_URL_ENV: str = "OMNIINTELLIGENCE_DB_URL"

#: Table read/updated by both consuming nodes.
CODE_ENTITIES_TABLE: str = "code_entities"

_POOL_MIN_SIZE = 1
_POOL_MAX_SIZE = 5
_COMMAND_TIMEOUT_SECONDS = 30

# Source-derived + enrichment columns the enrichment handler's prompt builder
# reads. Kept as an explicit projection (never SELECT *) so a schema change that
# drops a consumed column fails loudly here instead of producing a KeyError deep
# inside the handler.
_ENRICHMENT_COLUMNS = (
    "id, entity_name, entity_type, qualified_name, source_repo, "
    "source_path, docstring, signature, bases, methods, fields, decorators"
)

# Columns the embedding handler reads: primary embedding text fields plus the
# Qdrant point payload fields.
_EMBEDDING_COLUMNS = (
    "id, entity_name, entity_type, qualified_name, source_repo, "
    "source_path, docstring, signature, classification, llm_description"
)


class RepositoryCodeEntityPostgres:
    """Postgres implementation of ``ProtocolCodeEntityRepository``.

    Structural conformance only — the protocol is a ``Protocol``, not a base
    class, so there is no inheritance here (and no ``Plugin*`` base class;
    CLAUDE.md rule 7a).

    Args:
        pool: Pre-built asyncpg pool. Injected by tests and by any caller that
            already owns a pool; when ``None`` the pool is created lazily from
            *dsn*.
        dsn: Connection string. Defaults to ``os.environ`` lookup of
            ``OMNIINTELLIGENCE_DB_URL`` at *first use*, not at construction.
    """

    def __init__(
        self,
        *,
        pool: asyncpg.Pool | None = None,
        dsn: str | None = None,
    ) -> None:
        self._pool: asyncpg.Pool | None = pool
        self._owns_pool = pool is None
        self._pool_adapter: AsyncpgAdapter | None = None
        self._dsn = dsn

    # -- connection -----------------------------------------------------

    def _resolve_dsn(self) -> str:
        """Return the DSN, raising when it is neither injected nor configured."""
        if self._dsn:
            return self._dsn
        dsn = os.environ.get(  # contract-config-ok: config
            CODE_ENTITY_DB_URL_ENV, ""
        )
        if not dsn:
            raise OSError(
                f"{CODE_ENTITY_DB_URL_ENV} is required but not set. "
                f"Set it to the DSN of the database owning the "
                f"{CODE_ENTITIES_TABLE!r} table."
            )
        return dsn

    async def _get_pool(self) -> asyncpg.Pool:
        """Return the connection pool, creating it on first use."""
        if self._pool is not None:
            return self._pool

        adapter = AsyncpgAdapter(
            dsn=self._resolve_dsn(),
            min_size=_POOL_MIN_SIZE,
            max_size=_POOL_MAX_SIZE,
            command_timeout=_COMMAND_TIMEOUT_SECONDS,
        )
        await adapter.connect()
        pool = adapter.pool
        if pool is None:  # pragma: no cover - asyncpg only returns None on misuse
            raise OSError(
                f"asyncpg returned no pool for {CODE_ENTITY_DB_URL_ENV}; "
                "the DSN is present but unusable."
            )
        self._pool_adapter = adapter
        self._pool = pool
        return pool

    async def close(self) -> None:
        """Close the pool if this repository created it.

        An injected pool is owned by the caller and is left alone.
        """
        if self._pool_adapter is not None:
            await self._pool_adapter.close()
            self._pool_adapter = None
        elif self._pool is not None and self._owns_pool:
            await self._pool.close()
        self._pool = None

    # -- protocol methods -----------------------------------------------

    async def get_entities_needing_embedding(
        self, *, limit: int
    ) -> list[dict[str, Any]]:
        """Entities never embedded, or re-extracted since their last embedding."""
        _require_positive_limit(limit)
        pool = await self._get_pool()
        rows = await pool.fetch(
            f"""
            SELECT {_EMBEDDING_COLUMNS}
            FROM {CODE_ENTITIES_TABLE}
            WHERE last_embedded_at IS NULL
               OR last_embedded_at < last_extracted_at
            ORDER BY last_extracted_at ASC
            LIMIT $1
            """,
            limit,
        )
        return [dict(row) for row in rows]

    async def update_embedded_at(self, entity_ids: list[str]) -> None:
        """Stamp ``last_embedded_at`` for the given entities."""
        if not entity_ids:
            return
        pool = await self._get_pool()
        await pool.execute(
            f"""
            UPDATE {CODE_ENTITIES_TABLE} SET
                last_embedded_at = NOW(),
                updated_at = NOW()
            WHERE id = ANY($1::uuid[])
            """,
            entity_ids,
        )

    async def get_entities_needing_enrichment(
        self, *, limit: int
    ) -> list[dict[str, Any]]:
        """Entities that have never been classified."""
        _require_positive_limit(limit)
        pool = await self._get_pool()
        rows = await pool.fetch(
            f"""
            SELECT {_ENRICHMENT_COLUMNS}
            FROM {CODE_ENTITIES_TABLE}
            WHERE classification IS NULL
            ORDER BY last_extracted_at ASC
            LIMIT $1
            """,
            limit,
        )
        return [dict(row) for row in rows]

    async def update_enrichment(
        self,
        *,
        entity_id: str,
        classification: str,
        llm_description: str,
        architectural_pattern: str,
        classification_confidence: float,
        enrichment_version: str,
    ) -> None:
        """Persist one entity's enrichment result and stamp ``last_enriched_at``."""
        pool = await self._get_pool()
        await pool.execute(
            f"""
            UPDATE {CODE_ENTITIES_TABLE} SET
                classification = $2,
                llm_description = $3,
                architectural_pattern = $4,
                classification_confidence = $5,
                enrichment_version = $6,
                last_enriched_at = NOW(),
                updated_at = NOW()
            WHERE id = $1::uuid
            """,
            entity_id,
            classification,
            llm_description,
            architectural_pattern,
            classification_confidence,
            enrichment_version,
        )


def _require_positive_limit(limit: int) -> None:
    """Reject non-positive batch limits.

    ``LIMIT 0`` silently returns an empty batch, which the handlers read as
    "nothing to do" — indistinguishable from a genuinely drained queue. A
    negative limit is a Postgres syntax error. Both are caller bugs; fail loud.
    """
    if limit <= 0:
        raise ValueError(f"limit must be > 0, got {limit}")


__all__ = [
    "CODE_ENTITIES_TABLE",
    "CODE_ENTITY_DB_URL_ENV",
    "RepositoryCodeEntityPostgres",
]
