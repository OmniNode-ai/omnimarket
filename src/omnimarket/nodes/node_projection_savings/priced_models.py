# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Fill the shared savings_priced_models table from the pricing manifest (OMN-26444).

Decision D2-26444-A: the shared savings view applies priced_spend's named-price
rule in SQL, so the database holds each model the pricing manifest names and
whether it is free, the same rows the local store keeps in local_priced_models.
The view carries no literal price. The projection writer calls this before the
shared metering refresh folds, so the view and the fold read one manifest.
"""

from __future__ import annotations

from typing import Any, Protocol

from omnimarket.models.model_local_install_schema import local_priced_models_rows

# Unqualified, so the connection's search_path names the schema.
DELETE_SQL = "DELETE FROM savings_priced_models"
INSERT_SQL = "INSERT INTO savings_priced_models (model_key, free) VALUES ($1, $2)"


class ProtocolPricedModelsConnection(Protocol):
    """The part of an asyncpg connection the fill uses."""

    async def execute(self, query: str, *args: Any) -> str: ...

    async def executemany(self, command: str, args: Any) -> None: ...

    def transaction(self) -> Any: ...


async def refresh_shared_priced_models(conn: ProtocolPricedModelsConnection) -> int:
    """Replace the table's rows with the manifest's in one transaction; return the count."""
    rows = local_priced_models_rows()
    async with conn.transaction():
        await conn.execute(DELETE_SQL)
        await conn.executemany(INSERT_SQL, [(key, bool(free)) for key, free in rows])
    return len(rows)


__all__ = [
    "DELETE_SQL",
    "INSERT_SQL",
    "ProtocolPricedModelsConnection",
    "refresh_shared_priced_models",
]
