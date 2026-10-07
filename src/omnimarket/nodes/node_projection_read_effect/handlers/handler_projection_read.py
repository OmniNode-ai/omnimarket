# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Answer one projection read from the writer's table (OMN-20159).

The page is produced by
:func:`omnimarket.projection.read_page.read_projection_page`, the function the
standalone projection API's ``GET /projection/{topic}`` route calls, so this
node and that route cannot answer the same request differently. What this
handler adds is where its inputs come from: the exposures from the node
contracts' ``projection_api`` blocks, the database from the projection read
binding (the read overlay's, else the runtime's), and the tenant from the
command envelope or the request.
"""

from __future__ import annotations

from omnimarket.nodes.node_projection_read_effect.models import (
    ModelProjectionReadRequest,
    ModelProjectionReadResult,
)
from omnimarket.nodes.node_projection_read_effect.ports.read_source_resolution import (
    resolve_projection_read_source,
)
from omnimarket.nodes.node_projection_read_effect.ports.request_tenant import (
    TenantConflictError,
    resolve_request_tenant,
)
from omnimarket.nodes.node_projection_read_effect.ports.sqlite_row_source import (
    SqliteTableRowSource,
)
from omnimarket.projection.discovery import build_projection_topic_map
from omnimarket.projection.models import ProjectionTableConfig
from omnimarket.projection.read_page import (
    ProjectionPage,
    read_projection_page,
    read_refusal,
)
from omnimarket.projection.table_reader import (
    ProjectionReadError,
    ProtocolProjectionRowSource,
    TableRowSource,
)


def _result(topic: str, page: ProjectionPage) -> ModelProjectionReadResult:
    body = page.body
    if page.ok:
        return ModelProjectionReadResult(
            topic=topic,
            ok=True,
            http_status=page.status_code,
            row_count=body["row_count"],
            rows=body["rows"],
            tenant=body["tenant"],
            next_cursor=body["next_cursor"],
            truncated=body["truncated"],
            response=body,
        )
    # A degraded exposure's body names a reason, not an error code.
    error = body.get("error") or "projection_degraded"
    detail = body.get("detail") or body.get("reason") or body.get("degraded_reason")
    return ModelProjectionReadResult(
        topic=topic,
        ok=False,
        error=error,
        detail=None if detail is None else str(detail),
        http_status=page.status_code,
        response=body,
    )


class HandlerProjectionRead:
    """Typed read of one contract-declared projection exposure.

    ``topic_map`` and ``row_source`` are for tests; the runtime constructs the
    handler with neither, and both are resolved on first use: the exposures
    from the installed node contracts, the row source from the read binding.
    """

    def __init__(
        self,
        *,
        topic_map: dict[str, ProjectionTableConfig] | None = None,
        row_source: ProtocolProjectionRowSource | None = None,
    ) -> None:
        self._topic_map = topic_map
        self._row_source = row_source
        self._owned_source: TableRowSource | SqliteTableRowSource | None = None

    def _topics(self) -> dict[str, ProjectionTableConfig]:
        if self._topic_map is None:
            self._topic_map = build_projection_topic_map()
        return self._topic_map

    def _source(self) -> ProtocolProjectionRowSource:
        if self._row_source is not None:
            return self._row_source
        if self._owned_source is None:
            self._owned_source = resolve_projection_read_source()
        return self._owned_source

    async def close(self) -> None:
        """Close the pools of a row source this handler opened itself."""
        owned, self._owned_source = self._owned_source, None
        # A SQLite source holds no connection between reads.
        if isinstance(owned, TableRowSource):
            await owned.close()

    async def handle(
        self, request: ModelProjectionReadRequest
    ) -> ModelProjectionReadResult:
        try:
            tenant = resolve_request_tenant(request.tenant_id)
        except TenantConflictError as exc:
            return _result(
                request.topic,
                ProjectionPage(
                    422,
                    {
                        "error": "tenant_conflict",
                        "topic": request.topic,
                        "detail": str(exc),
                    },
                ),
            )
        try:
            source = self._source()
        except ProjectionReadError as exc:
            return _result(
                request.topic,
                ProjectionPage(exc.status_code, read_refusal(request.topic, exc)),
            )
        page = await read_projection_page(
            request.topic,
            topic_map=self._topics(),
            source=source,
            correlation_id=request.row_correlation_id,
            since=request.since,
            limit=request.limit,
            order=request.order,
            order_by=request.order_by,
            tenant=tenant,
        )
        return _result(request.topic, page)


__all__ = ["HandlerProjectionRead"]
