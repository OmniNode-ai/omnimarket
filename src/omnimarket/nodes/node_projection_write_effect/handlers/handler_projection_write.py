# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The generic projection write: persist a pure fold's typed result (OMN-18883).

A projection fold is a definition-B handler that returns
:class:`ModelProjectionFoldResult` and does no I/O. This effect handler writes
that result. Its database handle is a constructor dependency and the envelope
metadata arrives as a typed field of the request, so nothing is injected into an
event dict and a typed ``handle`` never validates and returns without writing.

Each row is one targeted-column UPSERT built by ``omnibase_core``'s
``build_upsert_plan`` and rendered for asyncpg. The statement returns the first
conflict column, so a row the conflict arm leaves alone (``DO NOTHING``) is not
counted, and ``rows_upserted`` is the number of rows the database reported.
The envelope's ``tenant_id`` is passed to the adapter as the caller-resolved
write tenant.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Literal

from omnibase_core.models.projection.model_upsert_plan import build_upsert_plan

from omnimarket.models.model_projection_write import (
    ModelProjectionFoldResult,
    ModelProjectionWriteRequest,
    ModelProjectionWriteResult,
    ProjectionCellValue,
)
from omnimarket.protocols.protocol_projection_write_database import (
    ProtocolProjectionWriteDatabase,
)

__all__ = ["HandlerProjectionWrite"]


def _bind(
    column: str, value: ProjectionCellValue, jsonb_columns: frozenset[str]
) -> object:
    if column in jsonb_columns:
        return json.dumps(value, sort_keys=True, default=str)
    return value


class HandlerProjectionWrite:
    """Write one fold result through the injected database handle."""

    handler_type: Literal["node_handler"] = "node_handler"
    handler_category: Literal["effect"] = "effect"

    def __init__(self, database: ProtocolProjectionWriteDatabase | None = None) -> None:
        # Optional so the runtime's constructor resolver can build the handler
        # (a required database parameter quarantines it as unresolvable); the
        # wiring binds the real handle, and a write without one fails loudly.
        self._database = database

    @staticmethod
    def statements(
        fold_result: ModelProjectionFoldResult,
    ) -> list[tuple[str, tuple[object, ...]]]:
        """Render every row's UPSERT before any connection is used."""
        jsonb = frozenset(fold_result.jsonb_columns)
        insert_only = frozenset(fold_result.insert_only_columns)
        returning = (fold_result.conflict_key.split(",")[0].strip(),)
        rendered: list[tuple[str, tuple[object, ...]]] = []
        for row in fold_result.rows:
            row_map: Mapping[str, ProjectionCellValue] = row
            plan = build_upsert_plan(
                table=fold_result.table,
                conflict_key=fold_result.conflict_key,
                row=row_map,
                insert_only_columns=insert_only,
                returning=returning,
            )
            statement = plan.render(
                dialect="numeric",
                jsonb_columns=jsonb.intersection(plan.bound_columns),
            )
            params = tuple(
                _bind(column, row_map[column], jsonb) for column in plan.bound_columns
            )
            rendered.append((statement, params))
        return rendered

    async def handle(
        self, request: ModelProjectionWriteRequest
    ) -> ModelProjectionWriteResult:
        """Persist ``request.fold_result`` and report the rows the database wrote."""
        database = self._database
        if database is None:
            raise RuntimeError(
                "HandlerProjectionWrite has no database handle bound; "
                "construct it with database=<ProtocolProjectionWriteDatabase>"
            )
        rendered = self.statements(request.fold_result)
        written = 0
        for statement, params in rendered:
            returned = await database.execute(
                statement, *params, tenant=request.envelope.tenant_id
            )
            written += len(returned)
        return ModelProjectionWriteResult(
            table=request.fold_result.table,
            envelope_id=request.envelope.envelope_id,
            rows_upserted=written,
        )
