# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Answer one declared projection query route (OMN-21288, T17 of the one-runtime plan).

This is the query logic the projection API service
(``omnimarket.projection.api_server``) carried in its route functions, moved
here so the service and the runtime-mounted node cannot answer the same request
differently: the service's routes call this handler too. Every read still goes
through :func:`omnimarket.projection.read_page.read_projection_page` and the
row source's tenant scoping, so a tenant-scoped exposure is served only under
a resolved tenant and the table read sets ``app.tenant_id`` for RLS.

Which routes exist, their paths, their query parameters and the evidence
topics each reads are declared in this node's ``contract.yaml``; nothing here
names a path or a topic.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any

from omnimarket.handlers.projection_ordering import sort_for_presentation
from omnimarket.nodes.node_projection_query_api_effect.models import (
    EnumQueryRouteKind,
    ModelProjectionQueryRequest,
    ModelProjectionQueryResponse,
    ModelQueryRoute,
)
from omnimarket.nodes.node_projection_query_api_effect.ports.contract_routes import (
    contract_route,
)
from omnimarket.projection.discovery import build_projection_topic_map
from omnimarket.projection.generation_publisher import publish_generation_request
from omnimarket.projection.models import (
    ProjectionStatus,
    ProjectionTableConfig,
    UnrankedOrderValueError,
)
from omnimarket.projection.morning_page import (
    DEFAULT_REFRESH_SECONDS,
    PAGE_TENANT_UUID,
    build_morning_page,
    render_morning_page,
)
from omnimarket.projection.read_page import (
    NOT_YET_BUS_BACKED_TICKET,
    PROJECTION_VERSION,
    compute_freshness,
    filter_rows,
    pagination_order_spec,
    read_projection_page,
    read_refusal,
    serving_tenant_scope,
    unranked_order_value_refusal,
)
from omnimarket.projection.table_reader import (
    ProjectionReadError,
    ProtocolProjectionRowSource,
    TableRowSource,
)


def _json(status_code: int, body: Any) -> ModelProjectionQueryResponse:
    return ModelProjectionQueryResponse(
        status_code=status_code, media_type="application/json", body=body
    )


def _column_value(row: dict[str, Any], column: str | None) -> Any:
    if column is None:
        return None
    return row.get(column)


class HandlerProjectionQuery:
    """Answers one contract-declared route of the projection query surface.

    ``topic_map`` and ``row_source`` are injected by the ASGI app and by tests;
    a handler built with neither resolves the exposures from the installed node
    contracts and reads through a :class:`TableRowSource` on first use, the way
    the service's lifespan did.
    """

    def __init__(
        self,
        *,
        topic_map: dict[str, ProjectionTableConfig] | None = None,
        row_source: ProtocolProjectionRowSource | None = None,
    ) -> None:
        self._topic_map = topic_map
        self._row_source = row_source
        self._owned_source: TableRowSource | None = None

    def _topics(self) -> dict[str, ProjectionTableConfig]:
        if self._topic_map is None:
            self._topic_map = build_projection_topic_map()
        return self._topic_map

    def _source(self) -> ProtocolProjectionRowSource:
        if self._row_source is not None:
            return self._row_source
        if self._owned_source is None:
            self._owned_source = TableRowSource()
        return self._owned_source

    async def close(self) -> None:
        """Close the pools of a row source this handler opened itself."""
        owned, self._owned_source = self._owned_source, None
        if owned is not None:
            await owned.close()

    async def handle(
        self, request: ModelProjectionQueryRequest
    ) -> ModelProjectionQueryResponse:
        route = contract_route(request.route_id)
        match route.kind:
            case EnumQueryRouteKind.HEALTH:
                return _json(200, self._source().health(self._topics()))
            case EnumQueryRouteKind.READY:
                ready, body = await self._source().readiness(self._topics())
                return _json(200 if ready else 503, body)
            case EnumQueryRouteKind.GENERATE:
                return await self._generate(request)
            case EnumQueryRouteKind.PROJECTIONS:
                return _json(200, {"topics": self._catalogue()})
            case EnumQueryRouteKind.STATUS_PAGE:
                return await self._status_page(request)
            case EnumQueryRouteKind.PROJECTION_PAGE:
                return await self._projection_page(request)
            case EnumQueryRouteKind.EVIDENCE_PAGE:
                return await self._evidence_page(route, request)
            case EnumQueryRouteKind.EVIDENCE_STREAM:
                return ModelProjectionQueryResponse(
                    status_code=200,
                    media_type=route.media_type,
                    text=(
                        "event: advisory\n"
                        'data: {"authority":"projection_state_required"}\n\n'
                    ),
                )

    async def _generate(
        self, request: ModelProjectionQueryRequest
    ) -> ModelProjectionQueryResponse:
        """Publish ONE generation command; the node_generation_consumer does the work."""
        if request.generate is None:
            return _json(422, {"detail": "generate request body is required"})
        try:
            generated = await publish_generation_request(request.generate)
        except RuntimeError as exc:
            # Broker not configured / unreachable: fail fast, no silent fallback.
            return _json(503, {"detail": str(exc)})
        return _json(200, generated.model_dump(mode="json"))

    def _catalogue(self) -> list[dict[str, Any]]:
        """Full metadata for every discovered projection topic."""
        return [
            {
                "topic": cfg.topic,
                "table": cfg.table,
                "schema": cfg.schema_name,
                "status": cfg.status if cfg.bus_backed else ProjectionStatus.DEGRADED,
                "columns": list(cfg.columns),
                "json_columns": list(cfg.json_columns),
                "order_by": cfg.order_by,
                "freshness_column": cfg.freshness_column,
                "cursor_column": cfg.cursor_column,
                "last_event_id_column": cfg.last_event_id_column,
                "last_ingest_sequence_column": cfg.last_ingest_sequence_column,
                "freshness_state_column": cfg.freshness_state_column,
                "degraded_reason_column": cfg.degraded_reason_column,
                "observed_at_column": cfg.observed_at_column,
                "limit": cfg.limit,
                "source_contract": cfg.source_contract,
                "degraded_reason": cfg.degraded_reason
                or (None if cfg.bus_backed else "not_yet_bus_backed"),
                "bus_backed": cfg.bus_backed,
                "key_columns": list(cfg.key_columns),
                "backing": "bus" if cfg.bus_backed else "not_yet_bus_backed",
                # ``backing`` keeps naming the writer class (a bus-fed writer is
                # deployed) because clients classify reachability on it; this
                # names where a read of the exposure is answered from.
                "served_from": "table" if cfg.bus_backed else None,
                "relation": (
                    f"{cfg.relation_schema}.{cfg.table}"
                    if cfg.relation_schema is not None
                    else None
                ),
                # A client must be able to discover that an exposure needs
                # ?tenant= from the catalogue, not from a 422 in production.
                "tenant_column": cfg.tenant_column,
                "tenant_scoped": cfg.tenant_scoped,
            }
            for cfg in self._topics().values()
        ]

    async def _status_page(
        self, request: ModelProjectionQueryRequest
    ) -> ModelProjectionQueryResponse:
        """The standing ONEX status page: ``200`` unconditionally.

        The page's own content carries per-panel health. Answering 5xx for the
        whole document because one exposure refused would hide the panels that
        are fine; the page IS the report, so it renders even when most of what it
        reports on is refusing. Every exposure it renders is read from its table
        once, up front, so the render itself stays synchronous.
        """
        topic_map = self._topics()
        view = await self._source().page_view(
            topic_map, tenant_id=str(PAGE_TENANT_UUID)
        )
        page = build_morning_page(
            topic_map,
            view,
            service_name=(
                os.environ.get("OTEL_SERVICE_NAME")
                or "projection-api (OTEL_SERVICE_NAME unset)"
            ),
            refresh_seconds=(
                request.refresh
                if request.refresh is not None
                else DEFAULT_REFRESH_SECONDS
            ),
        )
        return ModelProjectionQueryResponse(
            status_code=200,
            media_type="text/html",
            text=render_morning_page(page),
        )

    async def _projection_page(
        self, request: ModelProjectionQueryRequest
    ) -> ModelProjectionQueryResponse:
        # The page is read by the same function the runtime-resident
        # node_projection_read_effect calls, so the read paths cannot drift.
        page = await read_projection_page(
            request.topic or "",
            topic_map=self._topics(),
            source=self._source(),
            correlation_id=request.correlation_id,
            since=request.since,
            limit=request.limit,
            order=request.order,
            order_by=request.order_by,
            tenant=request.tenant,
        )
        return _json(page.status_code, page.body)

    async def _evidence_page(
        self, route: ModelQueryRoute, request: ModelProjectionQueryRequest
    ) -> ModelProjectionQueryResponse:
        topic = route.topic
        if topic is None:
            raise ValueError(f"evidence route {route.id!r} declares no topic")
        cfg = self._topics().get(topic)
        if cfg is None:
            return _json(
                503,
                {
                    "status": "degraded",
                    "error": "projection_not_configured",
                    "topic": topic,
                },
            )
        if cfg.status == ProjectionStatus.DEGRADED:
            return _json(503, {"status": "degraded", "reason": cfg.degraded_reason})
        if not cfg.bus_backed:
            return _json(
                503,
                {
                    "status": "degraded",
                    "error": "not_yet_bus_backed",
                    "topic": topic,
                    "migration_ticket": NOT_YET_BUS_BACKED_TICKET,
                },
            )
        if cfg.cursor_column is None:
            return _json(
                503,
                {
                    "status": "degraded",
                    "error": "cursor_column_missing",
                    "topic": topic,
                },
            )
        source = self._source()
        unavailable = source.unavailable(topic)
        if unavailable is not None:
            return _json(
                503,
                {"status": "degraded", "error": unavailable[0], "topic": topic},
            )

        # These routes expose no ``tenant`` query parameter, so an exposure that
        # declared a tenant_column could only be served here unscoped. Run the
        # same resolver with no caller-supplied value: a lane with a configured
        # tenant scopes to it, and a lane without one is refused rather than
        # answered unscoped.
        scope_tenant, tenant_refusal = await serving_tenant_scope(
            cfg, topic, None, source
        )
        if tenant_refusal is not None:
            return _json(tenant_refusal.status_code, tenant_refusal.body)

        effective_limit = min(request.limit or cfg.limit, cfg.limit)
        generated_at = datetime.now(UTC).isoformat()
        row_filtered = any(
            v is not None
            for v in (
                request.correlation_id,
                request.ticket_id,
                request.repo,
                request.pr_number,
            )
        )
        order_spec = pagination_order_spec(cfg, cfg.order_by_spec)
        # The whole served window: the cursor and content filters must run
        # before the page is cut.
        try:
            all_rows = await source.rows(
                cfg,
                order_spec=order_spec,
                tenant_id=scope_tenant,
                since=request.cursor,
                correlation_id=request.correlation_id,
            )
            latest_event_at = await source.latest_event_at(
                cfg,
                tenant_id=scope_tenant,
                window_rows=(
                    all_rows
                    if request.cursor is None and request.correlation_id is None
                    else None
                ),
            )
        except ProjectionReadError as exc:
            return _json(exc.status_code, read_refusal(topic, exc))
        filtered_rows = filter_rows(
            all_rows,
            cursor_column=cfg.cursor_column,
            cursor=request.cursor,
            correlation_id=request.correlation_id,
            ticket_id=request.ticket_id,
            repo=request.repo,
            pr_number=request.pr_number,
        )
        page_rows = filtered_rows[:effective_limit]
        try:
            serialisable_rows = sort_for_presentation(
                page_rows, cfg.order_by_spec, cfg.order_rank
            )
        except UnrankedOrderValueError as exc:
            return _json(503, unranked_order_value_refusal(topic, exc))

        latest_ts = latest_event_at.isoformat() if latest_event_at is not None else None
        latest_row = serialisable_rows[0] if serialisable_rows else {}
        # The test is truncation, not non-emptiness: a complete page that
        # happens to carry rows owes no cursor.
        next_cursor = (
            str(page_rows[-1].get(cfg.cursor_column))
            if len(filtered_rows) > effective_limit
            and page_rows
            and cfg.cursor_column in page_rows[-1]
            else None
        )
        computed_freshness = (
            "DEGRADED"
            if cfg.freshness_column is None
            else compute_freshness(
                latest_ts, cfg.expected_event_interval_seconds
            ).upper()
        )
        freshness_state: str = (
            "EMPTY"
            if (row_filtered and not serialisable_rows)
            else (
                _column_value(latest_row, cfg.freshness_state_column)
                or computed_freshness
            )
        )
        return _json(
            200,
            {
                "topic": topic,
                "version": PROJECTION_VERSION,
                "generated_at": generated_at,
                "query_scope": "evidence_pipeline",
                "authoritative_correlation_source": route.correlation_source,
                "projection_cursor": latest_row.get(cfg.cursor_column),
                "next_cursor": next_cursor,
                "last_event_id": _column_value(latest_row, cfg.last_event_id_column),
                "last_ingest_sequence": _column_value(
                    latest_row, cfg.last_ingest_sequence_column
                ),
                "freshness_state": freshness_state,
                "degraded_reason": _column_value(
                    latest_row, cfg.degraded_reason_column
                ),
                "observed_at": _column_value(latest_row, cfg.observed_at_column)
                or latest_ts,
                "row_count": len(serialisable_rows),
                "rows": serialisable_rows,
                "sse_authority": "advisory_only",
                "backing": source.backing,
                "staleness": source.staleness(topic, latest_ts),
            },
        )


__all__ = ["HandlerProjectionQuery"]
