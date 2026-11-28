# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Fill the shared savings_priced_models table from the pricing manifest (OMN-26444, decision FILL-26444).

The shared savings view applies priced_spend's named-price rule in SQL, so the database holds each model the pricing manifest names and whether it is free, the same rows the local store keeps in local_priced_models. The metering writer runs this through its own adapter before the shared metering refresh reads any run, so the view and the fold read one manifest. The table holds no tenant, so the write carries none, and the statement names no tenant_id. It lives here, a shared package, so the metering node does not import the savings node.
"""

from __future__ import annotations

from typing import Any, Protocol

from omnimarket.models.model_local_install_schema import local_priced_models_rows

# One statement, so the replace is atomic: the manifest's rows are upserted and every row the manifest no longer names is removed; the upserted and deleted rows are disjoint, so no key is both inserted and deleted.
FILL_SQL = """
WITH fresh AS (
    SELECT model_key, free
    FROM unnest($1::text[], $2::boolean[]) AS manifest(model_key, free)
), upserted AS (
    INSERT INTO public.savings_priced_models (model_key, free)
    SELECT model_key, free FROM fresh
    ON CONFLICT (model_key) DO UPDATE SET free = EXCLUDED.free
    RETURNING model_key
)
DELETE FROM public.savings_priced_models
WHERE model_key <> ALL($1::text[])
"""


class ProtocolPricedModelsWriter(Protocol):
    """The part of the writer's adapter the fill uses."""

    async def execute(
        self,
        query: str,
        *params: Any,
        tenant: str | None = None,
    ) -> list[dict[str, Any]]: ...


def priced_models_fill_args() -> tuple[list[str], list[bool]]:
    """The manifest's model keys and whether each is free, as the two arrays FILL_SQL takes."""
    rows = local_priced_models_rows()
    return [key for key, _ in rows], [bool(free) for _, free in rows]


async def refresh_shared_priced_models(db: ProtocolPricedModelsWriter) -> int:
    """Replace the table's rows with the manifest's in one statement, with no tenant; return the row count."""
    keys, frees = priced_models_fill_args()
    await db.execute(FILL_SQL, keys, frees, tenant=None)
    return len(keys)


__all__ = [
    "FILL_SQL",
    "ProtocolPricedModelsWriter",
    "priced_models_fill_args",
    "refresh_shared_priced_models",
]
