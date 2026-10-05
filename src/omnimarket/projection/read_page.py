# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One projection page read, shared by every path that serves one (OMN-20159).

The ``GET /projection/{topic}`` route of the standalone projection API and the
runtime-resident ``node_projection_read_effect`` answer the same question --
"the page of this contract-declared exposure for this tenant" -- and must give
the same answer. This module is that answer, written once: refusal checks,
tenant scoping, ordering, the served window, cursor paging and freshness. Each
caller only adapts it to its transport (an HTTP response, a typed result).

Every refusal is a body with a fixed ``error`` code and a status code; nothing
here raises for a request the caller can get wrong or for a table that cannot
answer.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import cmp_to_key
from typing import Any

from omnimarket.projection.discovery import parse_order_by_clauses
from omnimarket.projection.models import (
    ProjectionOrderRank,
    ProjectionStatus,
    ProjectionTableConfig,
    UnrankedOrderValueError,
)
from omnimarket.projection.table_reader import (
    ProjectionReadError,
    ProtocolProjectionRowSource,
)
from omnimarket.projection.tenant_isolation import (
    TenantContextMissingError,
    resolve_serving_tenant,
)

PROJECTION_VERSION = "1.0.0"
FRESH_THRESHOLD = timedelta(minutes=5)
STALE_THRESHOLD = timedelta(minutes=60)
NOT_YET_BUS_BACKED_TICKET = "OMN-15800"
TENANT_CONTEXT_TICKET = "OMN-15797"
# A FIXED string, never the exception's own text (security review, PR #2155):
# this endpoint is reachable by an external caller, and echoing internal
# exception detail back over HTTP is a leak channel. It is still the caller's
# remediation, not a bare status: a refusal that does not say what would make
# the request succeed just relocates the guessing the silent 200 caused.
TENANT_CONTEXT_DEGRADED_REASON = (
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
      arrived within ``FRESH_THRESHOLD``, otherwise ``idle``.
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
            return "fresh" if age < FRESH_THRESHOLD else "idle"
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


def base_order_by_spec(
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


def effective_order_by_spec(
    order_by_spec: tuple[tuple[str, str, str | None], ...], order: str | None
) -> tuple[tuple[str, str, str | None], ...]:
    """Apply a caller-requested direction flip to the FIRST sort column of
    ``order_by_spec`` (the contract default, or a caller-requested
    ``order_by`` override -- see :func:`base_order_by_spec`).

    Single source of truth for both the actual row order (fed to
    the row source's ``rows(order_spec=...)``) and the reported
    ``ordering`` string (:func:`reported_ordering`) -- computed once so the
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


def reported_ordering(
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


def cursor_compare(value: Any, cursor: str) -> bool:
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


def sort_for_presentation(
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


def unranked_order_value_refusal(
    topic: str, exc: UnrankedOrderValueError
) -> dict[str, Any]:
    """Typed refusal body for a row whose value the declared ``order_rank`` omits.

    ``503``: the caller cannot fix it by changing the request; the contract's
    rank must be extended to cover the value. Names the column, never the
    exception text.
    """
    return {
        "status": "degraded",
        "error": "unranked_order_value",
        "topic": topic,
        "column": exc.column,
    }


def pagination_order_spec(
    cfg: ProjectionTableConfig,
    presentation_order: tuple[tuple[str, str, str | None], ...],
) -> tuple[tuple[str, str, str | None], ...]:
    """Return the one stable ascending order used to select cursor pages."""
    if cfg.cursor_column is None:
        return presentation_order
    return ((cfg.cursor_column, "ASC", None),)


def filter_rows(
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
        result = [r for r in result if cursor_compare(r.get(cursor_column, ""), cursor)]
    if correlation_id is not None:
        result = [r for r in result if r.get("correlation_id") == correlation_id]
    if ticket_id is not None:
        result = [r for r in result if r.get("ticket_id") == ticket_id]
    if repo is not None:
        result = [r for r in result if r.get("repo") == repo]
    if pr_number is not None:
        result = [r for r in result if r.get("pr_number") == pr_number]
    return result


def tenant_scope(
    cfg: ProjectionTableConfig, topic: str, requested_tenant: str | None
) -> tuple[str | None, dict[str, Any] | None]:
    """Resolve the tenant this read is served under, or the refusal body.

    OMN-15797 AC2. Returns ``(tenant, None)`` when the read may proceed
    (``tenant`` is ``None`` for an exposure that declares no ``tenant_column``
    -- unchanged, unscoped serving), or ``(None, body)`` with the typed
    refusal. There is no third outcome: an exposure that declares a tenant
    column is either scoped to a resolved tenant or refused.

    Both refusals are ``422``, not ``503``: the caller can fix either one by
    changing the request (supply a tenant, or drop a ``tenant`` the exposure
    cannot honour). The ``503``s (``not_yet_bus_backed``) describe server state
    the caller cannot influence, which is the distinction being kept.
    """
    if not cfg.tenant_scoped:
        if requested_tenant is not None:
            # Never silently drop a scoping parameter: the caller would read
            # the resulting unscoped 200 as scoped. Same defect class as
            # OMN-16290's silently-ignored order_by, with a security edge.
            return None, {
                "error": "unsupported_filter",
                "filter": "tenant",
                "topic": topic,
                "detail": (
                    f"Topic '{topic}' declares no 'tenant_column' and "
                    "cannot be scoped by tenant; its rows are not "
                    "tenant-partitioned."
                ),
            }
        return None, None

    try:
        return resolve_serving_tenant(requested_tenant, topic=topic), None
    except TenantContextMissingError:
        return None, {
            "status": "degraded",
            "error": "tenant_context_unresolved",
            "topic": topic,
            "tenant_column": cfg.tenant_column,
            "degraded_reason": TENANT_CONTEXT_DEGRADED_REASON,
            "migration_ticket": TENANT_CONTEXT_TICKET,
        }


def read_refusal(topic: str, exc: ProjectionReadError) -> dict[str, Any]:
    """The refusal body for a read the table could not answer (OMN-20152)."""
    return {
        "status": "degraded",
        "error": exc.code,
        "topic": topic,
        "detail": exc.detail,
    }


@dataclass(frozen=True)
class ProjectionPage:
    """One read's outcome: the status code and the body every path returns."""

    status_code: int
    body: dict[str, Any]

    @property
    def ok(self) -> bool:
        return self.status_code == 200


async def read_projection_page(
    topic: str,
    *,
    topic_map: dict[str, ProjectionTableConfig],
    source: ProtocolProjectionRowSource,
    correlation_id: str | None = None,
    since: str | None = None,
    limit: int | None = None,
    order: str | None = None,
    order_by: str | None = None,
    tenant: str | None = None,
) -> ProjectionPage:
    """The page of one contract-declared exposure, or the named refusal."""
    if topic not in topic_map:
        return ProjectionPage(
            404,
            {
                "error": "unknown_topic",
                "available_topics": list(topic_map.keys()),
            },
        )

    cfg = topic_map[topic]

    if cfg.status == ProjectionStatus.DEGRADED:
        return ProjectionPage(
            503, {"status": "degraded", "reason": cfg.degraded_reason}
        )

    # OMN-15800: a family that has not converted to bus_backed yet returns an
    # explicit, self-documenting refusal -- never a stale/absent DB read. This
    # is the state every one of the other ~55 exposures is in today.
    if not cfg.bus_backed:
        return ProjectionPage(
            503,
            {
                "status": "degraded",
                "error": "not_yet_bus_backed",
                "topic": topic,
                "migration_ticket": NOT_YET_BUS_BACKED_TICKET,
            },
        )

    unavailable = source.unavailable(topic)
    if unavailable is not None:
        return ProjectionPage(
            503, {"status": "degraded", "error": unavailable[0], "topic": topic}
        )

    scope_tenant, tenant_refusal = tenant_scope(cfg, topic, tenant)
    if tenant_refusal is not None:
        return ProjectionPage(422, tenant_refusal)

    if correlation_id is not None and not topic_supports_correlation_id_filter(cfg):
        return ProjectionPage(
            422,
            {
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
        return ProjectionPage(
            422,
            {
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
        base_spec = base_order_by_spec(cfg, order_by)
    except InvalidOrderByError as exc:
        return ProjectionPage(
            422,
            {
                "error": "invalid_order_by",
                "topic": topic,
                "detail": str(exc),
            },
        )

    effective_limit = resolve_effective_limit(limit, cfg.limit)
    generated_at = datetime.now(UTC).isoformat()

    order_by_spec = effective_order_by_spec(base_spec, order)
    # OMN-17215 AC4: the declared rank belongs to the contract default. A
    # caller-supplied order_by replaces that default entirely, rank included.
    order_rank = cfg.order_rank if order_by is None else None
    page_order_spec = pagination_order_spec(cfg, order_by_spec)
    # OMN-17215: the whole served window, not the contract limit -- the
    # `since` filter and the truncation test below must see the whole set, or
    # no page at the contract limit advertises a cursor and no walk passes that
    # many rows. OMN-20152: the window is read from the writer's table.
    # OMN-20327: for an exposure that declares a cursor, a read without
    # ``since`` is page one of the ascending cursor walk over every key, not
    # the newest ``limit * 4`` rows -- a walk that starts inside the newest
    # window can never reach the keys older than it. OMN-19971: an exposure
    # with no cursor cannot walk (``since`` is refused above, so no page after
    # the first exists); it serves its newest rows, as the cache did. A ranked
    # exposure ranks the WHOLE set in the source, not a recency cut.
    ranked_window = cfg.page_selection == "order_by" and since is None
    if ranked_window:
        selection = "ranked"
    elif cfg.cursor_column is not None:
        selection = "walk"
    else:
        selection = "newest"
    try:
        all_rows = await source.rows(
            cfg,
            order_spec=order_by_spec if ranked_window else page_order_spec,
            tenant_id=scope_tenant,
            since=since,
            correlation_id=correlation_id,
            selection=selection,
        )
        # OMN-19971: one window read per page. The unfiltered newest window
        # already holds the newest row; any other window leaves the source to
        # read the newest value with a bounded query, never a second window.
        latest_event_at = await source.latest_event_at(
            cfg,
            tenant_id=scope_tenant,
            window_rows=(
                all_rows if selection == "newest" and correlation_id is None else None
            ),
        )
    except ProjectionReadError as exc:
        return ProjectionPage(exc.status_code, read_refusal(topic, exc))
    filtered_rows = filter_rows(
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
    try:
        if ranked_window:
            serialisable_rows = sort_for_presentation(
                filtered_rows, order_by_spec, order_rank
            )[:effective_limit]
            page_rows = serialisable_rows
        else:
            page_rows = filtered_rows[:effective_limit]
            serialisable_rows = sort_for_presentation(
                page_rows, order_by_spec, order_rank
            )
    except UnrankedOrderValueError as exc:
        return ProjectionPage(503, unranked_order_value_refusal(topic, exc))
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
    # OMN-19841: a ranked window cannot continue from its own last row -- its
    # rows are scattered across cursor space, so that value would continue a
    # walk this page never started and skip rows. OMN-20327: but a truncated
    # ranked page that says nothing leaves a walker with the top page as the
    # whole key set. It advertises the origin of the ascending walk instead:
    # the walk that follows starts at the first row, skips none, and repeats
    # only rows the ranked page already served.
    next_cursor: str | None = None
    if cfg.cursor_column is not None and truncated:
        if ranked_window:
            try:
                next_cursor = await source.walk_origin(cfg, tenant_id=scope_tenant)
            except ProjectionReadError as exc:
                return ProjectionPage(exc.status_code, read_refusal(topic, exc))
        else:
            last_cursor_val = page_rows[-1].get(cfg.cursor_column)
            if last_cursor_val is not None:
                next_cursor = str(last_cursor_val)

    return ProjectionPage(
        200,
        {
            "topic": topic,
            "projection_version": PROJECTION_VERSION,
            "generated_at": generated_at,
            "data_freshness": freshness,
            "ordering": reported_ordering(order_by_spec, order_rank),
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
        },
    )
