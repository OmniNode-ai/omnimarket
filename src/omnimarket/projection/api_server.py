"""Projection Query API Server (OMN-10461 / OMN-10490 / OMN-15800 / OMN-20152).

FastAPI server on port 3002 serving typed projection snapshots.

OMN-20152 (operator, 2026-09-30: "One delegation row is bullshit because
everything should be gathered from projections all that information is in the
fucking database."): every route reads the materialized projection table the
exposure's writer persists, through
:class:`~omnimarket.projection.table_reader.TableRowSource`. The in-memory
Kafka-fed ``SnapshotCache`` this process used to serve from (OMN-15800) lost
every row on a restart once it resumed from committed offsets, and it could
not answer until the broker's partition metadata resolved; neither can happen
to a read of the writer's durable table. This process holds no Kafka consumer.

Topic configuration is contract-driven: each projection node's contract.yaml
declares a ``projection_api`` section. An exposure is served once its contract
declares ``projection_api.bus_backed: true`` -- the declaration that a writer is
deployed and materializing it. An exposure whose writer is not declared yet
returns an explicit ``503 not_yet_bus_backed``, never a silent empty ``200``
(the failure mode OMN-15797 hid behind).

OMN-15797 AC2: an exposure whose contract declares ``projection_api.
tenant_column`` is served ONLY under a resolved tenant. A request whose tenant
context cannot be resolved returns ``422 tenant_context_unresolved``, and a
``?tenant=`` on an exposure with no tenant column returns ``422
unsupported_filter`` rather than being silently dropped. The table read scopes
by the row's own tenant column and sets ``app.tenant_id`` for RLS.

There is no hardcoded topic whitelist. The single source of truth is the
contract.yaml files discovered via ``onex.nodes`` entry points.
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from functools import cmp_to_key
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from omnimarket.projection.discovery import (
    build_projection_topic_map,
    parse_order_by_clauses,
)
from omnimarket.projection.generation_publisher import (
    ModelGenerateRequest,
    ModelGenerateResponse,
    publish_generation_request,
)
from omnimarket.projection.models import (
    ProjectionOrderRank,
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
from omnimarket.projection.table_reader import (
    ProjectionReadError,
    ProtocolProjectionRowSource,
    TableRowSource,
)
from omnimarket.projection.tenant_isolation import (
    TenantContextMissingError,
    resolve_serving_tenant,
)

log = logging.getLogger(__name__)

_PROJECTION_VERSION = "1.0.0"
_FRESH_THRESHOLD = timedelta(minutes=5)
_STALE_THRESHOLD = timedelta(minutes=60)
_NOT_YET_BUS_BACKED_TICKET = "OMN-15800"
_TENANT_CONTEXT_TICKET = "OMN-15797"
# A FIXED string, never the exception's own text (security review, PR #2155):
# this endpoint is reachable by an external caller, and echoing internal
# exception detail back over HTTP is a leak channel. It is still the caller's
# remediation, not a bare status: a refusal that does not say what would make
# the request succeed just relocates the guessing the silent 200 caused.
_TENANT_CONTEXT_DEGRADED_REASON = (
    "this exposure is tenant-scoped and no tenant context was resolved for the "
    "request; supply ?tenant=<id>"
)


def topic_supports_correlation_id_filter(cfg: ProjectionTableConfig) -> bool:
    """Return True when the topic's declared columns include ``correlation_id``.

    ``("*",)`` (SELECT *) is treated as supporting all filters because the
    underlying row shape may include that column even if it is not
    enumerated. For an explicit column list, ``correlation_id`` must appear
    verbatim — aggregate/summary exposures use a row shape with no per-row
    ``correlation_id`` and must not accept a filter on that column
    (OMN-13165).
    """
    if cfg.columns == ("*",):
        return True
    bare_columns = {col.strip('"') for col in cfg.columns}
    return "correlation_id" in bare_columns


def compute_freshness(
    latest_ts: str | None,
    expected_event_interval_seconds: int | None = None,
) -> str:
    """Classify projection freshness against the contract-declared cadence.

    Honest tri-state freshness (OMN-13035 / retro B-7) — silence is no longer
    treated as a failure for topics that are not expected to emit on a fixed
    cadence:

    * ``degraded`` — ``latest_ts is None``: the projection has no rows at all
      (a genuine query / materialization problem, NOT mere quiet).
    * On-demand topics (``expected_event_interval_seconds is None``): a topic
      that emits only when triggered. Silence is a normal, honest state, so it
      NEVER reports ``stale`` from no traffic. Returns ``fresh`` when a row
      arrived within ``_FRESH_THRESHOLD``, otherwise ``idle``.
    * Cadenced topics (``expected_event_interval_seconds > 0``): the contract
      declares an expected inter-event interval. Returns ``fresh`` while inside
      one interval, ``idle`` for one missed beat (``interval <= age <
      2*interval`` — quiet but not yet alarming), and ``stale`` only once the
      projection is genuinely behind its declared cadence (``age >=
      2*interval``).

    ``idle`` is a first-class state distinct from ``stale``: it means "no recent
    traffic, and that is expected", retiring the cry-wolf staleness label.
    """
    if latest_ts is None:
        return "degraded"
    try:
        ts_str = latest_ts
        if ts_str.endswith("+00:00"):
            ts_str = ts_str[:-6] + "Z"
        if ts_str.endswith("Z"):
            ts_str = ts_str[:-1] + "+00:00"
        ts = datetime.fromisoformat(ts_str)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        age = datetime.now(UTC) - ts
        if expected_event_interval_seconds is None:
            # On-demand: silence is honest, never stale.
            return "fresh" if age < _FRESH_THRESHOLD else "idle"
        interval = timedelta(seconds=expected_event_interval_seconds)
        if age < interval:
            return "fresh"
        if age < 2 * interval:
            return "idle"
        return "stale"
    except (ValueError, TypeError):
        return "degraded"


def resolve_effective_limit(requested: int | None, contract_limit: int) -> int:
    """Clamp a caller-requested ``limit`` to the contract-declared ceiling.

    ``None`` (no request) yields the contract limit. A positive request is
    bounded by ``contract_limit`` so a caller can shrink — but never enlarge —
    the result window. Non-positive requests are treated as unset.
    """
    if requested is None or requested <= 0:
        return contract_limit
    return min(requested, contract_limit)


class InvalidOrderByError(ValueError):
    """A request-time ``order_by`` query value failed to parse or validate.

    Distinct from :class:`~omnimarket.projection.discovery.MalformedOrderBySpecError`
    (OMN-16290): that one is a contract-authoring defect and hard-fails
    startup; this one is untrusted caller input on a live request and must
    map to a ``422``, never crash the process or silently fall back to the
    contract default (no-defensive-defaults).
    """


def _base_order_by_spec(
    cfg: ProjectionTableConfig, order_by: str | None
) -> tuple[tuple[str, str, str | None], ...]:
    """Resolve the order_by spec a request should sort by, BEFORE the
    ``order`` direction flip is applied.

    ``order_by=None`` (the common case) yields the contract-declared default
    (``cfg.order_by_spec``), preserving prior behavior exactly. A caller-
    supplied ``order_by`` (OMN-16290 -- previously accepted by FastAPI as an
    unrecognised query param and silently dropped, so every request served
    the fixed contract ordering regardless of what was requested) REPLACES
    the default entirely with the parsed, column-validated request spec.
    Raises :class:`InvalidOrderByError` on a malformed clause or a column not
    declared on this topic -- rejected outright, never silently ignored or
    coerced to the default (no-defensive-defaults).
    """
    if order_by is None:
        return cfg.order_by_spec
    try:
        return parse_order_by_clauses(order_by, cfg.columns)
    except ValueError as exc:
        raise InvalidOrderByError(str(exc)) from exc


def _effective_order_by_spec(
    order_by_spec: tuple[tuple[str, str, str | None], ...], order: str | None
) -> tuple[tuple[str, str, str | None], ...]:
    """Apply a caller-requested direction flip to the FIRST sort column of
    ``order_by_spec`` (the contract default, or a caller-requested
    ``order_by`` override -- see :func:`_base_order_by_spec`).

    Single source of truth for both the actual row order (fed to
    the row source's ``rows(order_spec=...)``) and the reported
    ``ordering`` string (:func:`_reported_ordering`) -- computed once so the
    two can never diverge (CodeRabbit, OMN-15800: a caller-requested ``order``
    previously changed only the reported string, not the returned rows).
    Every sort key beyond the first is preserved verbatim (OMN-15799), the
    NULLS placement included (OMN-15800 defect A corrective round) -- a
    caller-requested direction flip changes ASC/DESC only, never the
    declared NULLS FIRST|LAST for that column.
    """
    if not order_by_spec:
        return ()
    first_column, first_direction, first_nulls = order_by_spec[0]
    if order is not None:
        normalised = order.strip().lower()
        if normalised == "asc":
            first_direction = "ASC"
        elif normalised == "desc":
            first_direction = "DESC"
    return ((first_column, first_direction, first_nulls), *order_by_spec[1:])


def _reported_ordering(
    order_by_spec: tuple[tuple[str, str, str | None], ...],
    order_rank: ProjectionOrderRank | None = None,
) -> str:
    """Render the ``ordering`` response field FROM the typed, already-flipped spec.

    A declared ``order_rank`` (OMN-17215) is rendered first, as the SQL term it
    stands for, because it is the leading key the page is actually sorted by.
    """
    terms = [] if order_rank is None else [order_rank.sql_order_term()]
    terms.extend(
        f"{column} {direction}" + (f" NULLS {nulls}" if nulls else "")
        for column, direction, nulls in order_by_spec
    )
    if not terms:
        return "undefined"
    return ", ".join(terms)


def _cursor_compare(value: Any, cursor: str) -> bool:
    """Return ``True`` when ``value > cursor``, comparing numerically when both
    sides parse as numbers and falling back to string comparison otherwise.

    OMN-15800 (CodeRabbit): the removed SQL path compared a cursor column
    using its declared Postgres type; a naive ``str(value) > cursor`` is
    lexicographic and silently mis-orders numeric cursors (``"10" > "9"`` is
    ``False`` as strings).
    """
    text = str(value)
    try:
        return float(text) > float(cursor)
    except ValueError:
        return text > cursor


def _sort_for_presentation(
    rows: list[dict[str, Any]],
    order_by_spec: tuple[tuple[str, str, str | None], ...],
    order_rank: ProjectionOrderRank | None = None,
) -> list[dict[str, Any]]:
    """Sort an already-paged result for display without changing its cursor walk.

    Cursor pagination is always evaluated in ascending cursor order.  The
    contract's order is presentation-only and may be descending; applying it
    after the page is selected prevents a descending display from making the
    next-page predicate run backwards.

    A declared ``order_rank`` (OMN-17215 AC4) is the leading key, ahead of
    ``order_by_spec``. Every row's rank is resolved before sorting, so a value
    the rank does not declare raises :class:`UnrankedOrderValueError` rather
    than landing at an implicit position.
    """
    if not order_by_spec and order_rank is None:
        return rows

    # Resolve every row's rank before sorting, so an unranked value raises even
    # when the comparator would never have consulted that row.
    ranks: dict[int, int] = (
        {}
        if order_rank is None
        else {id(row): order_rank.rank_of(row.get(order_rank.column)) for row in rows}
    )

    def compare(left: dict[str, Any], right: dict[str, Any]) -> int:
        if order_rank is not None:
            rank_delta = ranks[id(left)] - ranks[id(right)]
            if rank_delta:
                return rank_delta
        for column, direction, nulls in order_by_spec:
            a, b = left.get(column), right.get(column)
            if a is None or b is None:
                if a is b:
                    continue
                nulls_first = nulls == "FIRST"
                # Preserve the cache's contract semantics: an omitted NULLS
                # clause keeps nulls last independent of ASC/DESC.
                result = (
                    (-1 if a is None else 1)
                    if nulls_first
                    else (1 if a is None else -1)
                )
                return result
            try:
                result = -1 if a < b else (1 if a > b else 0)
            except TypeError:
                sa, sb = str(a), str(b)
                result = -1 if sa < sb else (1 if sa > sb else 0)
            if result:
                return -result if direction == "DESC" else result
        return 0

    return sorted(rows, key=cmp_to_key(compare))


def _unranked_order_value_refusal(
    topic: str, exc: UnrankedOrderValueError
) -> JSONResponse:
    """Typed refusal for a row whose value the declared ``order_rank`` omits.

    ``503``: the caller cannot fix it by changing the request; the contract's
    rank must be extended to cover the value. Names the column, never the
    exception text.
    """
    return JSONResponse(
        status_code=503,
        content={
            "status": "degraded",
            "error": "unranked_order_value",
            "topic": topic,
            "column": exc.column,
        },
    )


def _pagination_order_spec(
    cfg: ProjectionTableConfig,
    presentation_order: tuple[tuple[str, str, str | None], ...],
) -> tuple[tuple[str, str, str | None], ...]:
    """Return the one stable ascending order used to select cursor pages."""
    if cfg.cursor_column is None:
        return presentation_order
    return ((cfg.cursor_column, "ASC", None),)


def _filter_rows(
    rows: list[dict[str, Any]],
    *,
    cursor_column: str | None,
    cursor: str | None,
    correlation_id: str | None,
    ticket_id: str | None,
    repo: str | None,
    pr_number: int | None,
) -> list[dict[str, Any]]:
    """In-memory content filter over cached rows -- no SQL, no DB (OMN-15800).

    Mirrors the filter set the pre-conversion SQL WHERE clauses supported so a
    family that converts later needs no route-shape change, only its contract
    flipped to ``bus_backed: true``.
    """
    result = rows
    if cursor_column is not None and cursor is not None:
        result = [
            r for r in result if _cursor_compare(r.get(cursor_column, ""), cursor)
        ]
    if correlation_id is not None:
        result = [r for r in result if r.get("correlation_id") == correlation_id]
    if ticket_id is not None:
        result = [r for r in result if r.get("ticket_id") == ticket_id]
    if repo is not None:
        result = [r for r in result if r.get("repo") == repo]
    if pr_number is not None:
        result = [r for r in result if r.get("pr_number") == pr_number]
    return result


def resolve_tenant_scope(
    cfg: ProjectionTableConfig, topic: str, requested_tenant: str | None
) -> tuple[str | None, JSONResponse | None]:
    """Resolve the tenant this request is served under, or the refusal to send.

    OMN-15797 AC2. Returns ``(tenant, None)`` when the request may proceed
    (``tenant`` is ``None`` for an exposure that declares no ``tenant_column``
    -- unchanged, unscoped serving), or ``(None, response)`` with the typed
    refusal to return instead. There is no third outcome: an exposure that
    declares a tenant column is either scoped to a resolved tenant or refused.

    Both refusals are ``422``, not ``503``: the caller can fix either one by
    changing the request (supply ``?tenant=``, or drop a ``tenant`` the
    exposure cannot honour). The ``503``s this module already emits
    (``not_yet_bus_backed``, ``snapshot_bootstrap_incomplete``) describe server
    state the caller cannot influence, which is the distinction being kept.
    """
    if not cfg.tenant_scoped:
        if requested_tenant is not None:
            # Never silently drop a scoping parameter: the caller would read
            # the resulting unscoped 200 as scoped. Same defect class as
            # OMN-16290's silently-ignored order_by, with a security edge.
            return None, JSONResponse(
                status_code=422,
                content={
                    "error": "unsupported_filter",
                    "filter": "tenant",
                    "topic": topic,
                    "detail": (
                        f"Topic '{topic}' declares no 'tenant_column' and "
                        "cannot be scoped by tenant; its rows are not "
                        "tenant-partitioned."
                    ),
                },
            )
        return None, None

    try:
        return resolve_serving_tenant(requested_tenant, topic=topic), None
    except TenantContextMissingError:
        return None, JSONResponse(
            status_code=422,
            content={
                "status": "degraded",
                "error": "tenant_context_unresolved",
                "topic": topic,
                "tenant_column": cfg.tenant_column,
                "degraded_reason": _TENANT_CONTEXT_DEGRADED_REASON,
                "migration_ticket": _TENANT_CONTEXT_TICKET,
            },
        )


# ---------------------------------------------------------------------------
# Module-level state — built once at startup, immutable after that
# ---------------------------------------------------------------------------

_topic_map: dict[str, ProjectionTableConfig] = {}
_row_source: TableRowSource | None = None


@asynccontextmanager
async def _lifespan(application: FastAPI) -> AsyncIterator[None]:
    global _topic_map, _row_source

    _topic_map = build_projection_topic_map()
    served_count = sum(1 for c in _topic_map.values() if c.bus_backed)
    log.info(
        "Projection topic map built at startup (restart required to refresh): "
        "%d topic(s) registered, %d served from their materialized tables",
        len(_topic_map),
        served_count,
    )
    # OMN-20152: constructing the row source connects to nothing, so the HTTP
    # server answers from its first second. A database that is down makes
    # reads refuse by name; it never holds startup.
    _row_source = TableRowSource()
    try:
        yield
    finally:
        if _row_source is not None:
            await _row_source.close()
            _row_source = None


app = FastAPI(
    title="Projection Query API", version=_PROJECTION_VERSION, lifespan=_lifespan
)


def _cors_origins_from_env() -> list[str]:
    raw = os.environ.get("PROJECTION_API_CORS_ORIGINS") or os.environ.get(
        "CORS_ORIGINS", ""
    )
    return [origin.strip() for origin in raw.split(",") if origin.strip()]


def _configure_cors(application: FastAPI) -> None:
    origins = _cors_origins_from_env()
    if not origins:
        log.info(
            "Projection API CORS not configured; browser reads are same-origin only"
        )
        return
    if "*" in origins:
        log.warning(
            "Projection API CORS configured with wildcard origin '*'; "
            "restrict this in production"
        )
    application.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
    )


_configure_cors(app)


# ---------------------------------------------------------------------------
# Dependencies — swapped by tests via dependency_overrides
# ---------------------------------------------------------------------------


def get_topic_map() -> dict[str, ProjectionTableConfig]:
    """Return the startup-pinned topic map.

    Tests override this via ``app.dependency_overrides[get_topic_map]``.
    """
    return _topic_map


def get_row_source() -> ProtocolProjectionRowSource:
    if _row_source is None:
        raise RuntimeError("projection row source not initialised")
    return _row_source


def _read_refusal(topic: str, exc: ProjectionReadError) -> JSONResponse:
    """The typed refusal for a read the table could not answer (OMN-20152)."""
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "status": "degraded",
            "error": exc.code,
            "topic": topic,
            "detail": exc.detail,
        },
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/health")
async def health(
    source: ProtocolProjectionRowSource = Depends(get_row_source),  # noqa: B008
    topic_map: dict[str, ProjectionTableConfig] = Depends(get_topic_map),  # noqa: B008
) -> JSONResponse:
    """Liveness only: the process is up. Touches no database and no broker."""
    return JSONResponse(source.health(topic_map))


@app.get("/ready")
async def readiness(
    source: ProtocolProjectionRowSource = Depends(get_row_source),  # noqa: B008
    topic_map: dict[str, ProjectionTableConfig] = Depends(get_topic_map),  # noqa: B008
) -> JSONResponse:
    """Fail closed unless every served exposure can be read.

    OMN-20152: an exposure is readable when the relation its writer
    materializes can be selected from through the DSN it is bound to. Each
    exposure that cannot is named with the reason, so the answer to "which
    panel is dark and why" is this response, not a trip to the database.
    """
    ready, body = await source.readiness(topic_map)
    return JSONResponse(body, status_code=200 if ready else 503)


@app.post("/api/generate")
async def generate_node(request: ModelGenerateRequest) -> ModelGenerateResponse:
    """Thin publisher: wrap the typed request in the canonical envelope and
    publish ONE command to ``onex.cmd.omnimarket.node-generation-requested.v1``.

    Returns the minted correlation id; the existing node_generation_consumer
    does the work and the SEA Control Plane projection renders the result.  No
    generation or state synthesis happens here.
    """
    try:
        return await publish_generation_request(request)
    except RuntimeError as exc:
        # Broker not configured / unreachable — fail-fast, no silent fallback.
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/projections")
async def list_projections(
    topic_map: dict[str, ProjectionTableConfig] = Depends(get_topic_map),  # noqa: B008
) -> JSONResponse:
    """Return full metadata for every discovered projection topic."""
    topics = [
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
            # OMN-20152: ``backing`` keeps naming the writer class (a bus-fed
            # writer is deployed) because clients classify reachability on it;
            # this names where a read of the exposure is answered from.
            "served_from": "table" if cfg.bus_backed else None,
            "relation": (
                f"{cfg.relation_schema}.{cfg.table}"
                if cfg.relation_schema is not None
                else None
            ),
            # OMN-15797 AC2: a client must be able to discover that an
            # exposure needs ?tenant= from the catalogue, not from a 422 in
            # production.
            "tenant_column": cfg.tenant_column,
            "tenant_scoped": cfg.tenant_scoped,
        }
        for cfg in topic_map.values()
    ]
    return JSONResponse({"topics": topics})


async def _render_status_page(
    refresh: int,
    topic_map: dict[str, ProjectionTableConfig],
    source: ProtocolProjectionRowSource,
) -> HTMLResponse:
    """Build and render the status page (OMN-17197, always-on per OMN-17346).

    Both ``GET /`` and ``GET /morning`` land here, so the alias is identical by
    construction rather than by two templates that agree today. The lane label
    is this process's own service identity — a page cannot be asked to claim a
    lane it is not running in.
    """
    # OMN-20152: every exposure the page renders is read from its table once,
    # up front, so the page itself stays a synchronous render of that data.
    view = await source.page_view(topic_map, tenant_id=str(PAGE_TENANT_UUID))
    page = build_morning_page(
        topic_map,
        view,
        service_name=(
            os.environ.get("OTEL_SERVICE_NAME")
            or "projection-api (OTEL_SERVICE_NAME unset)"
        ),
        refresh_seconds=refresh,
    )
    return HTMLResponse(render_morning_page(page))


@app.get("/", response_class=HTMLResponse)
async def status_page(
    refresh: int = Query(default=DEFAULT_REFRESH_SECONDS, ge=5, le=3600),
    topic_map: dict[str, ProjectionTableConfig] = Depends(get_topic_map),  # noqa: B008
    source: ProtocolProjectionRowSource = Depends(get_row_source),  # noqa: B008
) -> HTMLResponse:
    """The standing ONEX status page, at the root (OMN-17346).

    Operator ruling 2026-08-31: *"it should be always on and not require
    /morning. Index.html should be fine."* The root of the projection API used
    to answer ``404``, which meant the one always-up render of the live
    projections was reachable only by an operator who already knew a path — the
    OMN-14440 failure mode wearing a different hat. It is served from the same
    materialized tables every JSON route reads, because that is the only
    surface with no build step, no bundle, no session gate and no separate
    deployable between the projection and a human.

    ``200`` unconditionally, and deliberately: the page's own content carries
    per-panel health. Returning 5xx for the whole document because one exposure
    refused would hide the six panels that are fine — the page IS the report,
    so it must render even when most of what it reports on is refusing.
    """
    return await _render_status_page(refresh, topic_map, source)


@app.get("/morning", response_class=HTMLResponse)
async def morning_page(
    refresh: int = Query(default=DEFAULT_REFRESH_SECONDS, ge=5, le=3600),
    topic_map: dict[str, ProjectionTableConfig] = Depends(get_topic_map),  # noqa: B008
    source: ProtocolProjectionRowSource = Depends(get_row_source),  # noqa: B008
) -> HTMLResponse:
    """Alias of ``GET /`` kept for the links already on tickets (OMN-17346).

    It renders the page, it does not redirect to it: a redirect would break the
    already-published deep links behind a hop and would turn one request into
    two on a page that reloads itself every 30s.
    """
    return await _render_status_page(refresh, topic_map, source)


@app.get("/projection/{topic:path}")
async def projection_query(
    topic: str,
    correlation_id: str | None = Query(default=None),
    since: str | None = Query(default=None),
    limit: int | None = Query(default=None, ge=1),
    order: str | None = Query(default=None, pattern="^(?i:asc|desc)$"),
    order_by: str | None = Query(default=None),
    tenant: str | None = Query(default=None),
    topic_map: dict[str, ProjectionTableConfig] = Depends(get_topic_map),  # noqa: B008
    source: ProtocolProjectionRowSource = Depends(get_row_source),  # noqa: B008
) -> JSONResponse:
    if topic not in topic_map:
        return JSONResponse(
            status_code=404,
            content={
                "error": "unknown_topic",
                "available_topics": list(topic_map.keys()),
            },
        )

    cfg = topic_map[topic]

    if cfg.status == ProjectionStatus.DEGRADED:
        return JSONResponse(
            status_code=503,
            content={"status": "degraded", "reason": cfg.degraded_reason},
        )

    # OMN-15800: a family that has not converted to bus_backed yet returns an
    # explicit, self-documenting refusal -- never a stale/absent DB read. This
    # is the state every one of the other ~55 exposures is in today.
    if not cfg.bus_backed:
        return JSONResponse(
            status_code=503,
            content={
                "status": "degraded",
                "error": "not_yet_bus_backed",
                "topic": topic,
                "migration_ticket": _NOT_YET_BUS_BACKED_TICKET,
            },
        )

    unavailable = source.unavailable(topic)
    if unavailable is not None:
        return JSONResponse(
            status_code=503,
            content={"status": "degraded", "error": unavailable[0], "topic": topic},
        )

    scope_tenant, tenant_refusal = resolve_tenant_scope(cfg, topic, tenant)
    if tenant_refusal is not None:
        return tenant_refusal

    if correlation_id is not None and not topic_supports_correlation_id_filter(cfg):
        return JSONResponse(
            status_code=422,
            content={
                "error": "unsupported_filter",
                "filter": "correlation_id",
                "topic": topic,
                "detail": (
                    f"Topic '{topic}' does not expose a 'correlation_id' column "
                    "and cannot be filtered by it."
                ),
            },
        )

    if since is not None and cfg.cursor_column is None:
        return JSONResponse(
            status_code=422,
            content={
                "error": "unsupported_filter",
                "filter": "since",
                "topic": topic,
                "detail": (
                    f"Topic '{topic}' does not declare a 'cursor_column' and "
                    "cannot be paginated by 'since'."
                ),
            },
        )

    try:
        base_order_by_spec = _base_order_by_spec(cfg, order_by)
    except InvalidOrderByError as exc:
        return JSONResponse(
            status_code=422,
            content={
                "error": "invalid_order_by",
                "topic": topic,
                "detail": str(exc),
            },
        )

    effective_limit = resolve_effective_limit(limit, cfg.limit)
    generated_at = datetime.now(UTC).isoformat()

    order_by_spec = _effective_order_by_spec(base_order_by_spec, order)
    # OMN-17215 AC4: the declared rank belongs to the contract default. A
    # caller-supplied order_by replaces that default entirely, rank included.
    order_rank = cfg.order_rank if order_by is None else None
    pagination_order_spec = _pagination_order_spec(cfg, order_by_spec)
    # OMN-17215: the whole served window, not the contract limit -- the
    # `since` filter and the truncation test below must see the whole set, or
    # no page at the contract limit advertises a cursor and no walk passes that
    # many rows. OMN-20152: the window is read from the writer's table.
    try:
        all_rows = await source.rows(
            cfg,
            order_spec=pagination_order_spec,
            tenant_id=scope_tenant,
            since=since,
            correlation_id=correlation_id,
        )
        latest_event_at = await source.latest_event_at(
            cfg,
            tenant_id=scope_tenant,
            window_rows=(
                all_rows if since is None and correlation_id is None else None
            ),
        )
    except ProjectionReadError as exc:
        return _read_refusal(topic, exc)
    filtered_rows = _filter_rows(
        all_rows,
        cursor_column=cfg.cursor_column,
        cursor=since,
        correlation_id=correlation_id,
        ticket_id=None,
        repo=None,
        pr_number=None,
    )
    # OMN-19841: a ranked exposure (page_selection: order_by) answers a
    # request without ``since`` with the top ``limit`` rows by its declared
    # order, ranked over the WHOLE retained set before the cut. Cutting in
    # cursor order first and ranking the cut afterwards served the lowest
    # cursors -- the oldest rows -- for as long as the cache held more than
    # one page. A ``since`` request is a cursor walk under either selection.
    ranked_window = cfg.page_selection == "order_by" and since is None
    try:
        if ranked_window:
            serialisable_rows = _sort_for_presentation(
                filtered_rows, order_by_spec, order_rank
            )[:effective_limit]
            page_rows = serialisable_rows
        else:
            page_rows = filtered_rows[:effective_limit]
            serialisable_rows = _sort_for_presentation(
                page_rows, order_by_spec, order_rank
            )
    except UnrankedOrderValueError as exc:
        return _unranked_order_value_refusal(topic, exc)
    truncated = len(filtered_rows) > effective_limit

    latest_ts = latest_event_at.isoformat() if latest_event_at is not None else None
    freshness = (
        "unknown"
        if cfg.freshness_column is None
        else compute_freshness(latest_ts, cfg.expected_event_interval_seconds)
    )

    # OMN-17215: the test is truncation, not non-emptiness. A complete page that
    # happens to carry rows owes no cursor — advertising one sends the caller
    # after a page that is empty and indistinguishable from "more data".
    #
    # OMN-19841: a ranked window owes no cursor either, truncated or not. Its
    # rows are scattered across cursor space, so any value advertised here
    # would continue a walk this page never started and skip or repeat rows.
    # ``truncated`` states the fact the cursor would otherwise carry; a caller
    # that wants the whole set walks it with an explicit ``since``.
    next_cursor: str | None = None
    if cfg.cursor_column is not None and truncated and not ranked_window:
        last_cursor_val = page_rows[-1].get(cfg.cursor_column)
        if last_cursor_val is not None:
            next_cursor = str(last_cursor_val)

    return JSONResponse(
        {
            "topic": topic,
            "projection_version": _PROJECTION_VERSION,
            "generated_at": generated_at,
            "data_freshness": freshness,
            "ordering": _reported_ordering(order_by_spec, order_rank),
            "row_limit": effective_limit,
            "latest_event_at": latest_ts,
            "latest_projection_updated_at": latest_ts,
            "row_count": len(serialisable_rows),
            "next_cursor": next_cursor,
            # OMN-19841: which read this page answers -- "order_by" (the top
            # rows by the declared order) or "cursor" (one page of the
            # ascending cursor walk) -- and whether rows were left out of it.
            "page_selection": "order_by" if ranked_window else "cursor",
            "truncated": truncated,
            "rows": serialisable_rows,
            "backing": source.backing,
            # OMN-18905 / OMN-20152: whether the serving path has fallen
            # behind its source. A table read is the writer's durable state at
            # request time, so it states that; how far the writer is behind
            # shows in ``data_freshness``.
            "staleness": source.staleness(topic, latest_ts),
            # The tenant these rows are scoped to, or None for an exposure
            # that declares no tenant_column. Stated on the response so a
            # caller never has to assume which of the two it received.
            "tenant": scope_tenant,
        }
    )


@app.get("/v1/evidence-pipeline/dashboard")
@app.get("/v1/evidence-pipeline/stages")
async def evidence_pipeline_dashboard(
    cursor: str | None = Query(default=None),
    correlation_id: str | None = Query(default=None),
    ticket_id: str | None = Query(default=None),
    repo: str | None = Query(default=None),
    pr_number: int | None = Query(default=None),
    limit: int | None = Query(default=None, ge=1, le=500),
    topic_map: dict[str, ProjectionTableConfig] = Depends(get_topic_map),  # noqa: B008
    source: ProtocolProjectionRowSource = Depends(get_row_source),  # noqa: B008
) -> JSONResponse:
    return await _evidence_projection_response(
        topic="onex.snapshot.projection.evidence_pipeline.stages.v1",  # onex-topic-allow: projection-snapshot topic for evidence-pipeline API, no existing registry const (OMN-13944)
        cursor=cursor,
        correlation_id=correlation_id,
        ticket_id=ticket_id,
        repo=repo,
        pr_number=pr_number,
        limit=limit,
        topic_map=topic_map,
        source=source,
    )


@app.get("/v1/evidence-pipeline/correlation-traces")
@app.get("/v1/evidence-pipeline/correlations")
async def evidence_pipeline_correlation_traces(
    cursor: str | None = Query(default=None),
    correlation_id: str | None = Query(default=None),
    ticket_id: str | None = Query(default=None),
    repo: str | None = Query(default=None),
    pr_number: int | None = Query(default=None),
    limit: int | None = Query(default=None, ge=1, le=500),
    topic_map: dict[str, ProjectionTableConfig] = Depends(get_topic_map),  # noqa: B008
    source: ProtocolProjectionRowSource = Depends(get_row_source),  # noqa: B008
) -> JSONResponse:
    return await _evidence_projection_response(
        topic="onex.snapshot.projection.evidence_pipeline.correlations.v1",  # onex-topic-allow: projection-snapshot topic for evidence-pipeline API, no existing registry const (OMN-13944)
        cursor=cursor,
        correlation_id=correlation_id,
        ticket_id=ticket_id,
        repo=repo,
        pr_number=pr_number,
        limit=limit,
        topic_map=topic_map,
        source=source,
    )


@app.get("/v1/evidence-pipeline/readiness")
async def evidence_pipeline_readiness(
    cursor: str | None = Query(default=None),
    correlation_id: str | None = Query(default=None),
    ticket_id: str | None = Query(default=None),
    repo: str | None = Query(default=None),
    pr_number: int | None = Query(default=None),
    limit: int | None = Query(default=None, ge=1, le=500),
    topic_map: dict[str, ProjectionTableConfig] = Depends(get_topic_map),  # noqa: B008
    source: ProtocolProjectionRowSource = Depends(get_row_source),  # noqa: B008
) -> JSONResponse:
    return await _evidence_projection_response(
        topic="onex.snapshot.projection.evidence_pipeline.readiness.v1",  # onex-topic-allow: projection-snapshot topic for evidence-pipeline API, no existing registry const (OMN-13944)
        cursor=cursor,
        correlation_id=correlation_id,
        ticket_id=ticket_id,
        repo=repo,
        pr_number=pr_number,
        limit=limit,
        topic_map=topic_map,
        source=source,
    )


@app.get("/v1/evidence-pipeline/events")
async def evidence_pipeline_live_events(
    cursor: str | None = Query(default=None),
    correlation_id: str | None = Query(default=None),
    ticket_id: str | None = Query(default=None),
    repo: str | None = Query(default=None),
    pr_number: int | None = Query(default=None),
    limit: int | None = Query(default=None, ge=1, le=500),
    topic_map: dict[str, ProjectionTableConfig] = Depends(get_topic_map),  # noqa: B008
    source: ProtocolProjectionRowSource = Depends(get_row_source),  # noqa: B008
) -> JSONResponse:
    return await _evidence_projection_response(
        topic="onex.snapshot.projection.evidence_pipeline.live_events.v1",  # onex-topic-allow: projection-snapshot topic for evidence-pipeline API, no existing registry const (OMN-13944)
        cursor=cursor,
        correlation_id=correlation_id,
        ticket_id=ticket_id,
        repo=repo,
        pr_number=pr_number,
        limit=limit,
        topic_map=topic_map,
        source=source,
    )


@app.get("/v1/evidence-pipeline/events/stream")
async def evidence_pipeline_event_stream() -> StreamingResponse:
    """Advisory SSE endpoint.

    Reconnect clients must query the projection endpoints above for
    authoritative state; this stream is only a live-update hint.
    """

    async def _stream() -> AsyncIterator[str]:
        yield "event: advisory\n"
        yield 'data: {"authority":"projection_state_required"}\n\n'

    return StreamingResponse(_stream(), media_type="text/event-stream")


async def _evidence_projection_response(
    *,
    topic: str,
    cursor: str | None,
    correlation_id: str | None,
    ticket_id: str | None,
    repo: str | None,
    pr_number: int | None,
    limit: int | None,
    topic_map: dict[str, ProjectionTableConfig],
    source: ProtocolProjectionRowSource,
) -> JSONResponse:
    cfg = topic_map.get(topic)
    if cfg is None:
        return JSONResponse(
            status_code=503,
            content={
                "status": "degraded",
                "error": "projection_not_configured",
                "topic": topic,
            },
        )
    if cfg.status == ProjectionStatus.DEGRADED:
        return JSONResponse(
            status_code=503,
            content={"status": "degraded", "reason": cfg.degraded_reason},
        )
    if not cfg.bus_backed:
        return JSONResponse(
            status_code=503,
            content={
                "status": "degraded",
                "error": "not_yet_bus_backed",
                "topic": topic,
                "migration_ticket": _NOT_YET_BUS_BACKED_TICKET,
            },
        )
    if cfg.cursor_column is None:
        return JSONResponse(
            status_code=503,
            content={
                "status": "degraded",
                "error": "cursor_column_missing",
                "topic": topic,
            },
        )
    unavailable = source.unavailable(topic)
    if unavailable is not None:
        return JSONResponse(
            status_code=503,
            content={"status": "degraded", "error": unavailable[0], "topic": topic},
        )

    # OMN-15797 AC2: these routes expose no ``tenant`` query parameter, so an
    # exposure that declared a tenant_column could only be served here
    # unscoped. Run the same resolver with no caller-supplied value: a lane
    # with a configured tenant scopes to it, and a lane without one is refused
    # rather than answered unscoped. No evidence-pipeline exposure declares a
    # tenant_column today, so this is inert until one does -- which is the
    # point: it cannot be flipped on and quietly bypass the guard here.
    scope_tenant, tenant_refusal = resolve_tenant_scope(cfg, topic, None)
    if tenant_refusal is not None:
        return tenant_refusal

    effective_limit = min(limit or cfg.limit, cfg.limit)
    generated_at = datetime.now(UTC).isoformat()

    row_filtered = any(
        v is not None for v in (correlation_id, ticket_id, repo, pr_number)
    )

    pagination_order_spec = _pagination_order_spec(cfg, cfg.order_by_spec)
    # OMN-17215: the whole served window, for the same reason as
    # projection_query -- the cursor and content filters must run before the
    # page is cut. OMN-20152: read from the writer's table.
    try:
        all_rows = await source.rows(
            cfg,
            order_spec=pagination_order_spec,
            tenant_id=scope_tenant,
            since=cursor,
            correlation_id=correlation_id,
        )
        latest_event_at = await source.latest_event_at(
            cfg,
            tenant_id=scope_tenant,
            window_rows=(
                all_rows if cursor is None and correlation_id is None else None
            ),
        )
    except ProjectionReadError as exc:
        return _read_refusal(topic, exc)
    filtered_rows = _filter_rows(
        all_rows,
        cursor_column=cfg.cursor_column,
        cursor=cursor,
        correlation_id=correlation_id,
        ticket_id=ticket_id,
        repo=repo,
        pr_number=pr_number,
    )
    page_rows = filtered_rows[:effective_limit]
    try:
        serialisable_rows = _sort_for_presentation(
            page_rows, cfg.order_by_spec, cfg.order_rank
        )
    except UnrankedOrderValueError as exc:
        return _unranked_order_value_refusal(topic, exc)

    latest_ts = latest_event_at.isoformat() if latest_event_at is not None else None
    latest_row = serialisable_rows[0] if serialisable_rows else {}
    # OMN-18035: the test is truncation, not non-emptiness. A complete page that happens to
    # carry rows owes no cursor — advertising one sends the caller after a page that is empty
    # and indistinguishable from "more data". Same repair as OMN-17215 made on
    # projection_query; this is the sibling seam, serving /v1/evidence-pipeline/*.
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
        else compute_freshness(latest_ts, cfg.expected_event_interval_seconds).upper()
    )
    freshness_state: str = (
        "EMPTY"
        if (row_filtered and not serialisable_rows)
        else (
            _column_value(latest_row, cfg.freshness_state_column) or computed_freshness
        )
    )

    return JSONResponse(
        {
            "topic": topic,
            "version": _PROJECTION_VERSION,
            "generated_at": generated_at,
            "query_scope": "evidence_pipeline",
            "authoritative_correlation_source": "onex.snapshot.projection.delegation.correlation-trace.v1",  # onex-topic-allow: projection-snapshot topic for delegation correlation-trace API, no existing registry const (OMN-13944)
            "projection_cursor": latest_row.get(cfg.cursor_column),
            "next_cursor": next_cursor,
            "last_event_id": _column_value(latest_row, cfg.last_event_id_column),
            "last_ingest_sequence": _column_value(
                latest_row, cfg.last_ingest_sequence_column
            ),
            "freshness_state": freshness_state,
            "degraded_reason": _column_value(latest_row, cfg.degraded_reason_column),
            "observed_at": _column_value(latest_row, cfg.observed_at_column)
            or latest_ts,
            "row_count": len(serialisable_rows),
            "rows": serialisable_rows,
            "sse_authority": "advisory_only",
            "backing": source.backing,
            # OMN-18905 / OMN-20152: see projection_query.
            "staleness": source.staleness(topic, latest_ts),
        }
    )


def _column_value(row: dict[str, Any], column: str | None) -> Any:
    if column is None:
        return None
    return row.get(column)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def main() -> None:
    """Run the projection API server from an installed omnimarket package."""
    import uvicorn

    uvicorn.run(
        "omnimarket.projection.api_server:app",
        host="0.0.0.0",
        port=3002,
        reload=False,
    )


if __name__ == "__main__":
    main()
