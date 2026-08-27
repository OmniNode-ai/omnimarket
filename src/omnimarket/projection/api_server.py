"""Projection Query API Server (OMN-10461 / OMN-10490 / OMN-15800).

FastAPI server on port 3002 serving typed projection snapshots.

OMN-15800 (2026-08-09 operator ruling: "It should be accessing the
projections from the event bus not from a database. Nothing should be
connecting to a database other than the runtime."): this process holds ZERO
database driver and ZERO DSN. Every route is served from an in-memory
:class:`~omnimarket.projection.snapshot_cache.SnapshotCache` fed by a
background Kafka consumer reading the contract-declared, compacted
``onex.snapshot.projection.*`` topics.

Topic configuration is still contract-driven: each projection node's
contract.yaml declares a ``projection_api`` section. Conversion is a
strangler migration, per-exposure: a family is served from the bus only once
its contract declares ``projection_api.bus_backed: true`` (and the writer-
side reducer publishes to its snapshot topic). A family that has not
converted yet returns an explicit ``503 not_yet_bus_backed`` — never a stale
DB read, never a silent empty ``200`` (the failure mode OMN-15797 hid behind).

OMN-15797 AC2: an exposure whose contract declares ``projection_api.
tenant_column`` is served ONLY under a resolved tenant. A request whose tenant
context cannot be resolved returns ``422 tenant_context_unresolved``, and a
``?tenant=`` on an exposure with no tenant column returns ``422
unsupported_filter`` rather than being silently dropped. Between those two,
the serving path has no way to answer ``200`` with rows it could not honestly
scope — the property that let the original OMN-15797 defect survive
undetected.

There is no hardcoded topic whitelist. The single source of truth is the
contract.yaml files discovered via ``onex.nodes`` entry points.
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

from omnimarket.projection.discovery import (
    build_projection_topic_map,
    parse_order_by_clauses,
)
from omnimarket.projection.generation_publisher import (
    ModelGenerateRequest,
    ModelGenerateResponse,
    publish_generation_request,
)
from omnimarket.projection.models import ProjectionStatus, ProjectionTableConfig
from omnimarket.projection.runner import (
    KAFKA_BROKERS_ENV,
    projection_runtime_binding_from_overlay_env,
)
from omnimarket.projection.snapshot_cache import SnapshotCache
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
    ``SnapshotCache.get_rows(order_by_override=...)``) and the reported
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


def _reported_ordering(order_by_spec: tuple[tuple[str, str, str | None], ...]) -> str:
    """Render the ``ordering`` response field FROM the typed, already-flipped spec."""
    if not order_by_spec:
        return "undefined"
    return ", ".join(
        f"{column} {direction}" + (f" NULLS {nulls}" if nulls else "")
        for column, direction, nulls in order_by_spec
    )


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
_snapshot_cache: SnapshotCache | None = None


def _kafka_bootstrap_servers() -> str:
    """Resolve the Kafka bootstrap servers for the SnapshotCache's consumer.

    Same resolution order as :class:`omnimarket.projection.runner.BaseProjectionRunner`
    (overlay -> env -> Settings) so the serving process and the writer
    reducers agree on which broker to reach without either hardcoding a host.

    Raises ``RuntimeError`` when every source is empty (CodeRabbit,
    OMN-15800) -- fail fast with a clear configuration error rather than
    handing an empty broker list to ``AIOKafkaConsumer``, which fails far
    less legibly deep inside the client.
    """
    binding = projection_runtime_binding_from_overlay_env()
    if binding is not None and binding.kafka_bootstrap_servers.strip():
        return binding.kafka_bootstrap_servers

    from omnimarket.config.settings import Settings

    settings = Settings()
    resolved = (
        os.environ.get(KAFKA_BROKERS_ENV, "").strip()
        or settings.kafka_bootstrap_servers.strip()
        or settings.kafka_broker.strip()
    )
    if not resolved:
        raise RuntimeError(
            "projection-api requires Kafka bootstrap servers; none resolved "
            f"from a runtime binding overlay, {KAFKA_BROKERS_ENV}, or Settings"
        )
    return resolved


@asynccontextmanager
async def _lifespan(application: FastAPI) -> AsyncIterator[None]:
    global _topic_map, _snapshot_cache

    _topic_map = build_projection_topic_map()
    bus_backed_count = sum(1 for c in _topic_map.values() if c.bus_backed)
    log.info(
        "Projection topic map built at startup (restart required to refresh): "
        "%d topic(s) registered, %d bus_backed",
        len(_topic_map),
        bus_backed_count,
    )

    cache = SnapshotCache(_topic_map, bootstrap_servers=_kafka_bootstrap_servers())

    try:
        # Assign the module global BEFORE start() (CodeRabbit, OMN-15800): if
        # the broker is unreachable, self._consumer.start() raises after the
        # AIOKafkaConsumer object already exists. Assigning first, INSIDE this
        # try, means the `finally` below always reaches `cache` (via the same
        # module global) and stops the partially-started consumer instead of
        # leaking it -- a `finally` always runs when an exception propagates
        # through its `try` body, including one raised before `yield`.
        _snapshot_cache = cache
        await cache.start()
        yield
    finally:
        if _snapshot_cache is not None:
            await _snapshot_cache.stop()
            _snapshot_cache = None


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


def get_snapshot_cache() -> SnapshotCache:
    if _snapshot_cache is None:
        raise RuntimeError("SnapshotCache not initialised")
    return _snapshot_cache


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/health")
async def health(
    cache: SnapshotCache = Depends(get_snapshot_cache),  # noqa: B008
) -> JSONResponse:
    """Liveness only. OMN-15800: no DB to probe; reports the bus-backed
    topics this process's SnapshotCache is tracking."""
    return JSONResponse(
        {
            "status": "ok",
            "bus_backed_topics": sorted(cache.bus_backed_topics),
        }
    )


@app.get("/ready")
async def readiness(
    cache: SnapshotCache = Depends(get_snapshot_cache),  # noqa: B008
    topic_map: dict[str, ProjectionTableConfig] = Depends(get_topic_map),  # noqa: B008
) -> JSONResponse:
    """Fail closed unless every bus_backed exposure's SnapshotCache has
    finished its initial bootstrap replay (OMN-15800; replaces the removed
    ``SELECT 1`` Postgres probe)."""
    bus_backed_topics = sorted(t for t, cfg in topic_map.items() if cfg.bus_backed)
    bootstrap_status = {
        topic: cache.is_bootstrapped(topic) for topic in bus_backed_topics
    }
    ready = bool(bus_backed_topics) and all(bootstrap_status.values())
    return JSONResponse(
        {
            "status": "ready" if ready else "not_ready",
            "bus_backed_topics": bootstrap_status,
        },
        status_code=200 if ready else 503,
    )


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
            "status": cfg.status,
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
            "degraded_reason": cfg.degraded_reason or None,
            "bus_backed": cfg.bus_backed,
            "key_columns": list(cfg.key_columns),
            "backing": "bus" if cfg.bus_backed else "not_yet_bus_backed",
            # OMN-15797 AC2: a client must be able to discover that an
            # exposure needs ?tenant= from the catalogue, not from a 422 in
            # production.
            "tenant_column": cfg.tenant_column,
            "tenant_scoped": cfg.tenant_scoped,
        }
        for cfg in topic_map.values()
    ]
    return JSONResponse({"topics": topics})


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
    cache: SnapshotCache = Depends(get_snapshot_cache),  # noqa: B008
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

    if not cache.is_bootstrapped(topic):
        return JSONResponse(
            status_code=503,
            content={
                "status": "degraded",
                "error": "snapshot_bootstrap_incomplete",
                "topic": topic,
            },
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
    all_rows = cache.get_rows(
        topic,
        limit=None,
        order_by_override=order_by_spec,
        tenant_column=cfg.tenant_column,
        tenant_id=scope_tenant,
    )
    filtered_rows = _filter_rows(
        all_rows,
        cursor_column=cfg.cursor_column,
        cursor=since,
        correlation_id=correlation_id,
        ticket_id=None,
        repo=None,
        pr_number=None,
    )
    serialisable_rows = filtered_rows[:effective_limit]

    latest_event_at = cache.latest_event_at(topic)
    latest_ts = latest_event_at.isoformat() if latest_event_at is not None else None
    freshness = (
        "unknown"
        if cfg.freshness_column is None
        else compute_freshness(latest_ts, cfg.expected_event_interval_seconds)
    )

    next_cursor: str | None = None
    if cfg.cursor_column is not None and serialisable_rows:
        last_cursor_val = serialisable_rows[-1].get(cfg.cursor_column)
        if last_cursor_val is not None:
            next_cursor = str(last_cursor_val)

    return JSONResponse(
        {
            "topic": topic,
            "projection_version": _PROJECTION_VERSION,
            "generated_at": generated_at,
            "data_freshness": freshness,
            "ordering": _reported_ordering(order_by_spec),
            "row_limit": effective_limit,
            "latest_event_at": latest_ts,
            "latest_projection_updated_at": latest_ts,
            "row_count": len(serialisable_rows),
            "next_cursor": next_cursor,
            "rows": serialisable_rows,
            "backing": "bus",
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
    cache: SnapshotCache = Depends(get_snapshot_cache),  # noqa: B008
) -> JSONResponse:
    return _evidence_projection_response(
        topic="onex.snapshot.projection.evidence_pipeline.stages.v1",  # onex-topic-allow: projection-snapshot topic for evidence-pipeline API, no existing registry const (OMN-13944)
        cursor=cursor,
        correlation_id=correlation_id,
        ticket_id=ticket_id,
        repo=repo,
        pr_number=pr_number,
        limit=limit,
        topic_map=topic_map,
        cache=cache,
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
    cache: SnapshotCache = Depends(get_snapshot_cache),  # noqa: B008
) -> JSONResponse:
    return _evidence_projection_response(
        topic="onex.snapshot.projection.evidence_pipeline.correlations.v1",  # onex-topic-allow: projection-snapshot topic for evidence-pipeline API, no existing registry const (OMN-13944)
        cursor=cursor,
        correlation_id=correlation_id,
        ticket_id=ticket_id,
        repo=repo,
        pr_number=pr_number,
        limit=limit,
        topic_map=topic_map,
        cache=cache,
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
    cache: SnapshotCache = Depends(get_snapshot_cache),  # noqa: B008
) -> JSONResponse:
    return _evidence_projection_response(
        topic="onex.snapshot.projection.evidence_pipeline.readiness.v1",  # onex-topic-allow: projection-snapshot topic for evidence-pipeline API, no existing registry const (OMN-13944)
        cursor=cursor,
        correlation_id=correlation_id,
        ticket_id=ticket_id,
        repo=repo,
        pr_number=pr_number,
        limit=limit,
        topic_map=topic_map,
        cache=cache,
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
    cache: SnapshotCache = Depends(get_snapshot_cache),  # noqa: B008
) -> JSONResponse:
    return _evidence_projection_response(
        topic="onex.snapshot.projection.evidence_pipeline.live_events.v1",  # onex-topic-allow: projection-snapshot topic for evidence-pipeline API, no existing registry const (OMN-13944)
        cursor=cursor,
        correlation_id=correlation_id,
        ticket_id=ticket_id,
        repo=repo,
        pr_number=pr_number,
        limit=limit,
        topic_map=topic_map,
        cache=cache,
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


def _evidence_projection_response(
    *,
    topic: str,
    cursor: str | None,
    correlation_id: str | None,
    ticket_id: str | None,
    repo: str | None,
    pr_number: int | None,
    limit: int | None,
    topic_map: dict[str, ProjectionTableConfig],
    cache: SnapshotCache,
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
    if not cache.is_bootstrapped(topic):
        return JSONResponse(
            status_code=503,
            content={
                "status": "degraded",
                "error": "snapshot_bootstrap_incomplete",
                "topic": topic,
            },
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

    all_rows = cache.get_rows(
        topic,
        limit=None,
        tenant_column=cfg.tenant_column,
        tenant_id=scope_tenant,
    )
    filtered_rows = _filter_rows(
        all_rows,
        cursor_column=cfg.cursor_column,
        cursor=cursor,
        correlation_id=correlation_id,
        ticket_id=ticket_id,
        repo=repo,
        pr_number=pr_number,
    )
    serialisable_rows = filtered_rows[:effective_limit]

    latest_event_at = cache.latest_event_at(topic)
    latest_ts = latest_event_at.isoformat() if latest_event_at is not None else None
    latest_row = serialisable_rows[0] if serialisable_rows else {}
    next_cursor = (
        str(serialisable_rows[-1].get(cfg.cursor_column))
        if serialisable_rows and cfg.cursor_column in serialisable_rows[-1]
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
            "backing": "bus",
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
