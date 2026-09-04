# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ONEX status page (OMN-17197, made always-on by OMN-17346; epic OMN-16776).

Operator ruling, 2026-08-31: *"go with the always-up server-rendered page
straight off the projection API now; omnidash becomes the richer version
later."* Then, same day: *"it should be always on and not require /morning.
Index.html should be fine."*

That second ruling is why the page no longer calls itself a *morning* page. It
is the standing ops surface of the projection API: served at ``GET /`` with
``GET /morning`` kept as a byte-identical alias (links to it already exist on
tickets), titled ``ONEX Status — <lane>`` so a dev tab and a prod tab are
distinguishable at the tab strip, and carrying its ``as of`` timestamp in the
header chrome rather than in the fine print, because on an auto-refreshing page
the only thing standing between an operator and a stale read is that line.

This module is the render half of that ruling. It is **server-rendered off the
same in-process SnapshotCache the JSON routes serve** — no client fetch, no
client-side SQL, no client-side state derivation, no new database handle. The
page is HTML because HTML is always up: it needs no bundle, no build, no
session gate, and no separate deployable, so it cannot repeat the OMN-14440
failure mode where the projection is live and nothing renders it.

Two invariants make this page worth trusting, and both are enforced here rather
than left to the template:

1. **Truth is rendered, never computed.** ``flow_state`` is the verdict
   ``node_projection_consumer_flow`` derived; this module reorders and counts
   rows, and never regrades one. The only reduction performed is
   latest-window-per-consumer, which selects an existing row — it does not
   synthesise one.

2. **A refusal never renders as a zero.** Every panel carries the *exact*
   refusal taxonomy of ``GET /projection/{topic}`` (``unknown_topic``,
   ``contract_degraded``, ``not_yet_bus_backed``,
   ``snapshot_bootstrap_incomplete``, ``tenant_context_unresolved``) plus the
   ``no_rows`` case, and a panel that is not ``LIVE`` renders its reason and its
   migration ticket instead of a number. A dashboard that answers "0" when it
   means "I was refused" is the "one row of zeros over an empty table" shape
   epic OMN-16776 exists to eliminate — see the delegation-savings panel, which
   is wired to the real exposures and today renders exactly that refusal
   (OMN-15800 on this serving path, OMN-17298 behind it).
"""

from __future__ import annotations

import html
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict

from omnimarket.projection.models import ProjectionStatus, ProjectionTableConfig
from omnimarket.projection.snapshot_cache import SnapshotCache

# --------------------------------------------------------------------------
# The exposures this page reads. Every one is a contract-declared topic served
# by this same process; none is a table name and none is a URL.
# --------------------------------------------------------------------------
TOPIC_CONSUMER_FLOW = "onex.snapshot.projection.consumer-flow.v1"
TOPIC_REGISTRATION = "onex.snapshot.projection.registration.v1"
TOPIC_LIVE_EVENTS = "onex.snapshot.projection.live-events.v1"
TOPIC_SESSION_REPLAY = "onex.snapshot.projection.session.replay.v1"
# OMN-17772. Absent from the catalog entirely until this exposure stopped
# being excluded at discovery for declaring an unservable schema -- the page
# could not even refuse it, which is strictly worse than a refusal.
TOPIC_WORK_EVENTS = "onex.snapshot.projection.work.events.v1"
TOPIC_SKILL_EXECUTIONS = "onex.snapshot.projection.skill-executions.v1"
TOPIC_DELEGATION_SAVINGS = "onex.snapshot.projection.delegation.savings.v1"
TOPIC_COST_SAVINGS_OVERVIEW = "onex.snapshot.projection.cost.savings-overview.v1"
TOPIC_DELEGATION_SUMMARY = "onex.snapshot.projection.delegation.summary.v1"

#: Refresh cadence. The consumer-flow writer emits a window every ~30s, so a
#: faster refresh would render the same window twice and a slower one would let
#: a STALLED window age off the top of the page before anyone saw it.
DEFAULT_REFRESH_SECONDS = 30

#: The standing name of this surface (OMN-17346). The lane is appended from the
#: serving process's own service identity, which is per-lane
#: (``omnimarket-projection-api`` on dev,
#: ``omnimarket-stability-test-projection-api`` on stability,
#: ``omnimarket-prod-projection-api`` on prod), so a title alone tells an
#: operator which runtime they are looking at.
PAGE_NAME = "ONEX Status"

#: How many rows to pull per exposure. consumer-flow publishes one row per
#: (consumer_group, topic) per window, and the live fleet is ~500 pairs; the
#: cache holds the compacted latest-per-key set, so this is a render cap, not a
#: sampling window.
_FLOW_ROW_CAP = 2000
_LIST_ROW_CAP = 25

_TICKET_NOT_BUS_BACKED = "OMN-15800"
_TICKET_TENANT = "OMN-15797"
_TICKET_DELEGATION_ROWS = "OMN-17298"
_TICKET_PAGE = "OMN-17197"
_TICKET_ALWAYS_ON = "OMN-17346"

#: Severity order for the attention table. STALLED first because it is the
#: OMN-16755 shape (consumer up, lag zero, output topic flat); UNKNOWN ranks
#: above IDLE because a dropped window is not evidence of quiet.
_FLOW_STATE_RANK: dict[str, int] = {
    "STALLED": 0,
    "STARVED": 1,
    "UNKNOWN": 2,
    "FLOWING": 3,
    "IDLE": 4,
}


class EnumPanelState(StrEnum):
    """Whether a panel is showing data, showing nothing, or showing a refusal."""

    #: The exposure answered and returned at least one row.
    LIVE = "LIVE"
    #: The exposure answered and holds no rows. Honest emptiness, not a zero.
    EMPTY = "EMPTY"
    #: The serving path refused. Carries the refusal code and its ticket.
    REFUSED = "REFUSED"


class ModelProjectionRead(BaseModel):
    """One exposure read, with the refusal taxonomy carried alongside the rows."""

    model_config = ConfigDict(frozen=True)

    topic: str
    state: EnumPanelState
    reason_code: str
    reason_detail: str
    migration_ticket: str | None
    rows: tuple[dict[str, Any], ...]
    latest_event_at: str | None
    cached_row_count: int


class ModelFlowConsumer(BaseModel):
    """One (consumer_group, topic) seam at its most recent observed window."""

    model_config = ConfigDict(frozen=True)

    consumer_group: str
    topic: str
    flow_state: str
    messages_in: int
    messages_out: int
    messages_dlq: int
    handler_errors: int
    upstream_evidence: str
    window_end: str


class ModelFlowPanel(BaseModel):
    """Event-flow panel: state census, DLQ depth, and the seams needing attention."""

    model_config = ConfigDict(frozen=True)

    read: ModelProjectionRead
    consumer_count: int
    state_counts: tuple[tuple[str, int], ...]
    dlq_total: int
    handler_error_total: int
    attention: tuple[ModelFlowConsumer, ...]
    idle_count: int


class ModelSavingsMetric(BaseModel):
    """One delegation-savings number, always carrying the exposure it came from."""

    model_config = ConfigDict(frozen=True)

    label: str
    value: str
    source_topic: str


class ModelSavingsPanel(BaseModel):
    """Delegation savings: local-vs-cloud cost, wired to the real exposures.

    The operator's staging-acceptance bar names "the dashboard with delegation
    savings". This panel is that bar's surface. It reads the three real
    exposures and, when they refuse, renders the refusal — it never falls back
    to a fixture, an estimate, or a zero.
    """

    model_config = ConfigDict(frozen=True)

    reads: tuple[ModelProjectionRead, ...]
    metrics: tuple[ModelSavingsMetric, ...]

    @property
    def has_data(self) -> bool:
        return bool(self.metrics)


class ModelInventoryRow(BaseModel):
    """One line of the exposure census — what the serving path would answer."""

    model_config = ConfigDict(frozen=True)

    topic: str
    backing: str
    source_contract: str
    cached_row_count: int
    latest_event_at: str | None
    tenant_scoped: bool


class ModelMorningPage(BaseModel):
    """The whole page as data, so every panel is assertable without parsing HTML."""

    model_config = ConfigDict(frozen=True)

    generated_at: str
    service_name: str
    refresh_seconds: int
    exposure_count: int
    bus_backed_count: int
    flow: ModelFlowPanel
    savings: ModelSavingsPanel
    registry: ModelProjectionRead
    live_events: ModelProjectionRead
    work_events: ModelProjectionRead
    sessions: ModelProjectionRead
    skill_executions: ModelProjectionRead
    inventory: tuple[ModelInventoryRow, ...]


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------


def read_projection(
    topic: str,
    topic_map: dict[str, ProjectionTableConfig],
    cache: SnapshotCache,
    *,
    limit: int,
) -> ModelProjectionRead:
    """Read one exposure, mirroring ``GET /projection/{topic}``'s refusals exactly.

    The refusal branches are deliberately the same set, in the same order, as
    :func:`omnimarket.projection.api_server.projection_query`. A page that
    invented a softer taxonomy would let an exposure look healthier here than
    it is over the API — the divergence being rendered would be the page's own.
    """
    cfg = topic_map.get(topic)
    if cfg is None:
        return ModelProjectionRead(
            topic=topic,
            state=EnumPanelState.REFUSED,
            reason_code="unknown_topic",
            reason_detail=(
                "no contract in this process's discovered topic map declares "
                "this exposure"
            ),
            migration_ticket=None,
            rows=(),
            latest_event_at=None,
            cached_row_count=0,
        )

    if cfg.status == ProjectionStatus.DEGRADED:
        return ModelProjectionRead(
            topic=topic,
            state=EnumPanelState.REFUSED,
            reason_code="contract_degraded",
            reason_detail=cfg.degraded_reason
            or "contract declared this exposure degraded",
            migration_ticket=None,
            rows=(),
            latest_event_at=None,
            cached_row_count=0,
        )

    if not cfg.bus_backed:
        return ModelProjectionRead(
            topic=topic,
            state=EnumPanelState.REFUSED,
            reason_code="not_yet_bus_backed",
            reason_detail=(
                "this exposure has not converted to the bus-fed serving path; "
                "the projection API holds no database handle, so there is no "
                "second place it could read from"
            ),
            migration_ticket=_TICKET_NOT_BUS_BACKED,
            rows=(),
            latest_event_at=None,
            cached_row_count=0,
        )

    if cfg.tenant_scoped:
        # The page is an unauthenticated operator surface with no tenant
        # context to resolve. Serving a tenant-scoped exposure here would
        # either leak across tenants or render one tenant's rows as the
        # platform's. Refuse, and say which.
        return ModelProjectionRead(
            topic=topic,
            state=EnumPanelState.REFUSED,
            reason_code="tenant_context_unresolved",
            reason_detail=(
                f"exposure is scoped by '{cfg.tenant_column}' and this page "
                "resolves no tenant context"
            ),
            migration_ticket=_TICKET_TENANT,
            rows=(),
            latest_event_at=None,
            cached_row_count=0,
        )

    if not cache.is_bootstrapped(topic):
        return ModelProjectionRead(
            topic=topic,
            state=EnumPanelState.REFUSED,
            reason_code="snapshot_bootstrap_incomplete",
            reason_detail=(
                "the snapshot consumer has not finished its initial replay of "
                "the compacted topic"
            ),
            migration_ticket=None,
            rows=(),
            latest_event_at=None,
            cached_row_count=0,
        )

    rows = tuple(cache.get_rows(topic, limit=limit))
    latest = cache.latest_event_at(topic)
    return ModelProjectionRead(
        topic=topic,
        state=EnumPanelState.LIVE if rows else EnumPanelState.EMPTY,
        reason_code="ok" if rows else "no_rows",
        reason_detail=(
            ""
            if rows
            else (
                "the exposure is bus-backed and bootstrapped, and its compacted "
                "snapshot topic currently holds no rows"
            )
        ),
        migration_ticket=None,
        rows=rows,
        latest_event_at=latest.isoformat() if latest is not None else None,
        cached_row_count=cache.row_count(topic),
    )


def _as_int(row: dict[str, Any], key: str) -> int:
    value = row.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return 0
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _as_text(row: dict[str, Any], key: str) -> str:
    value = row.get(key)
    return "" if value is None else str(value)


def latest_window_per_consumer(
    rows: tuple[dict[str, Any], ...],
) -> tuple[ModelFlowConsumer, ...]:
    """Collapse a window stream to one row per (consumer_group, topic).

    Selection, not synthesis: the returned row is a row the projection wrote.
    Ordering key is ``(window_end, ingest_sequence)`` — ``window_end`` is the
    projection's own window boundary and ``ingest_sequence`` breaks the tie
    when two windows share a boundary timestamp, which happens because every
    consumer on one runtime shares that runtime's heartbeat window.
    """
    newest: dict[tuple[str, str], tuple[tuple[str, int], dict[str, Any]]] = {}
    for row in rows:
        key = (_as_text(row, "consumer_group"), _as_text(row, "topic"))
        rank = (_as_text(row, "window_end"), _as_int(row, "ingest_sequence"))
        existing = newest.get(key)
        if existing is None or rank > existing[0]:
            newest[key] = (rank, row)

    consumers = [
        ModelFlowConsumer(
            consumer_group=key[0],
            topic=key[1],
            flow_state=_as_text(row, "flow_state") or "UNKNOWN",
            messages_in=_as_int(row, "messages_in"),
            messages_out=_as_int(row, "messages_out"),
            messages_dlq=_as_int(row, "messages_dlq"),
            handler_errors=_as_int(row, "handler_errors"),
            upstream_evidence=_as_text(row, "upstream_evidence") or "NONE",
            window_end=_as_text(row, "window_end"),
        )
        for key, (_rank, row) in newest.items()
    ]
    consumers.sort(
        key=lambda c: (
            _FLOW_STATE_RANK.get(c.flow_state, 2),
            -c.messages_dlq,
            -c.handler_errors,
            c.consumer_group,
        )
    )
    return tuple(consumers)


def build_flow_panel(read: ModelProjectionRead) -> ModelFlowPanel:
    """Census + attention list for the event-flow panel.

    When the read is not ``LIVE`` every count is zero *and the panel state says
    so* — the renderer keys off ``read.state``, never off the counts, so a
    refusal can never reach the page as a clean set of zeros.
    """
    consumers = latest_window_per_consumer(read.rows)
    counts: dict[str, int] = {}
    for consumer in consumers:
        counts[consumer.flow_state] = counts.get(consumer.flow_state, 0) + 1
    ordered_counts = tuple(
        (state, counts[state])
        for state in sorted(counts, key=lambda s: _FLOW_STATE_RANK.get(s, 2))
    )
    attention = tuple(c for c in consumers if c.flow_state != "IDLE")
    return ModelFlowPanel(
        read=read,
        consumer_count=len(consumers),
        state_counts=ordered_counts,
        dlq_total=sum(c.messages_dlq for c in consumers),
        handler_error_total=sum(c.handler_errors for c in consumers),
        attention=attention,
        idle_count=counts.get("IDLE", 0),
    )


def _money(row: dict[str, Any], key: str) -> str | None:
    value = row.get(key)
    if value is None or isinstance(value, bool):
        return None
    try:
        return f"${float(value):,.4f}"
    except (TypeError, ValueError):
        return None


def _plain(row: dict[str, Any], key: str) -> str | None:
    value = row.get(key)
    if value is None or isinstance(value, dict | list):
        return None
    return str(value)


def build_savings_panel(reads: tuple[ModelProjectionRead, ...]) -> ModelSavingsPanel:
    """Assemble the delegation-savings numbers from whichever reads are LIVE.

    A metric is emitted only when its source exposure returned a row carrying
    that field. There is no default, no ``or 0``, and no cross-exposure
    inference: an absent number is absent from the page, and the panel's reads
    say why.
    """
    metrics: list[ModelSavingsMetric] = []
    by_topic = {read.topic: read for read in reads}

    savings = by_topic.get(TOPIC_DELEGATION_SAVINGS)
    if savings is not None and savings.state == EnumPanelState.LIVE:
        row = savings.rows[0]
        for label, rendered in (
            ("delegated sessions", _plain(row, "session_count")),
            ("local cost spent", _money(row, "cumulative_local_cost_usd")),
            (
                "cloud baseline (counterfactual)",
                _money(row, "cumulative_cloud_cost_usd"),
            ),
            ("cost avoided", _money(row, "cumulative_savings_usd")),
            ("baseline model", _plain(row, "baseline_model")),
            ("pricing manifest", _plain(row, "pricing_manifest_version")),
        ):
            if rendered is not None:
                metrics.append(
                    ModelSavingsMetric(
                        label=label, value=rendered, source_topic=savings.topic
                    )
                )

    overview = by_topic.get(TOPIC_COST_SAVINGS_OVERVIEW)
    if overview is not None and overview.state == EnumPanelState.LIVE:
        row = overview.rows[0]
        for label, rendered in (
            ("window", _plain(row, "window")),
            ("window cost spent", _money(row, "total_cost_usd")),
            ("window baseline", _money(row, "total_baseline_cost_usd")),
            ("window cost avoided", _money(row, "total_savings_usd")),
            ("savings rate", _plain(row, "savings_rate")),
            ("local token share", _plain(row, "local_token_pct")),
            ("measured runs", _plain(row, "measured_run_count")),
        ):
            if rendered is not None:
                metrics.append(
                    ModelSavingsMetric(
                        label=label, value=rendered, source_topic=overview.topic
                    )
                )

    summary = by_topic.get(TOPIC_DELEGATION_SUMMARY)
    if summary is not None and summary.state == EnumPanelState.LIVE:
        row = summary.rows[0]
        for label, rendered in (
            ("delegations recorded", _plain(row, "totalDelegations")),
            ("quality-gate pass rate", _plain(row, "qualityGatePassRate")),
            ("avg latency ms", _plain(row, "avg_latency_ms")),
        ):
            if rendered is not None:
                metrics.append(
                    ModelSavingsMetric(
                        label=label, value=rendered, source_topic=summary.topic
                    )
                )

    return ModelSavingsPanel(reads=reads, metrics=tuple(metrics))


def build_inventory(
    topic_map: dict[str, ProjectionTableConfig], cache: SnapshotCache
) -> tuple[ModelInventoryRow, ...]:
    """Every discovered exposure and what the serving path would answer for it.

    This census is itself the finding the page exists to surface: it makes the
    ratio of bus-backed exposures to declared ones visible every morning,
    rather than discoverable only by curling 59 endpoints.
    """
    rows: list[ModelInventoryRow] = []
    for topic, cfg in topic_map.items():
        bus_backed = cfg.bus_backed
        latest = cache.latest_event_at(topic) if bus_backed else None
        rows.append(
            ModelInventoryRow(
                topic=topic,
                backing="bus" if bus_backed else "not_yet_bus_backed",
                source_contract=cfg.source_contract,
                cached_row_count=cache.row_count(topic) if bus_backed else 0,
                latest_event_at=latest.isoformat() if latest is not None else None,
                tenant_scoped=cfg.tenant_scoped,
            )
        )
    rows.sort(key=lambda r: (r.backing != "bus", r.topic))
    return tuple(rows)


def build_morning_page(
    topic_map: dict[str, ProjectionTableConfig],
    cache: SnapshotCache,
    *,
    service_name: str,
    refresh_seconds: int = DEFAULT_REFRESH_SECONDS,
) -> ModelMorningPage:
    """Read every panel's exposure and assemble the page as data."""
    flow_read = read_projection(
        TOPIC_CONSUMER_FLOW, topic_map, cache, limit=_FLOW_ROW_CAP
    )
    savings_reads = tuple(
        read_projection(topic, topic_map, cache, limit=1)
        for topic in (
            TOPIC_DELEGATION_SAVINGS,
            TOPIC_COST_SAVINGS_OVERVIEW,
            TOPIC_DELEGATION_SUMMARY,
        )
    )
    return ModelMorningPage(
        generated_at=datetime.now(UTC).isoformat(),
        service_name=service_name,
        refresh_seconds=refresh_seconds,
        exposure_count=len(topic_map),
        bus_backed_count=sum(1 for cfg in topic_map.values() if cfg.bus_backed),
        flow=build_flow_panel(flow_read),
        savings=build_savings_panel(savings_reads),
        registry=read_projection(
            TOPIC_REGISTRATION, topic_map, cache, limit=_LIST_ROW_CAP
        ),
        live_events=read_projection(
            TOPIC_LIVE_EVENTS, topic_map, cache, limit=_LIST_ROW_CAP
        ),
        work_events=read_projection(
            TOPIC_WORK_EVENTS, topic_map, cache, limit=_LIST_ROW_CAP
        ),
        sessions=read_projection(
            TOPIC_SESSION_REPLAY, topic_map, cache, limit=_LIST_ROW_CAP
        ),
        skill_executions=read_projection(
            TOPIC_SKILL_EXECUTIONS, topic_map, cache, limit=_LIST_ROW_CAP
        ),
        inventory=build_inventory(topic_map, cache),
    )


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

_STYLE = """
:root{--bg:#0d1117;--panel:#161b22;--line:#30363d;--fg:#e6edf3;--dim:#8b949e;
--stalled:#f85149;--starved:#d29922;--flowing:#3fb950;--idle:#6e7681;--unknown:#a371f7}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:13px/1.5 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
header{padding:18px 22px;border-bottom:1px solid var(--line);
display:flex;flex-wrap:wrap;gap:6px 22px;align-items:baseline}
h1{font-size:15px;margin:0;letter-spacing:.06em;text-transform:uppercase}
header .meta{color:var(--dim);font-size:11px}
header .lane{color:var(--fg);font-size:12px;letter-spacing:.04em}
header .asof{color:var(--fg);font-size:12px;border:1px solid var(--line);
border-radius:4px;padding:2px 9px;background:var(--panel);margin-left:auto}
main{padding:18px 22px;display:flex;flex-direction:column;gap:18px}
section{border:1px solid var(--line);border-radius:6px;background:var(--panel)}
section>h2{margin:0;padding:10px 14px;border-bottom:1px solid var(--line);
font-size:11px;letter-spacing:.1em;text-transform:uppercase;color:var(--dim);
display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap}
.body{padding:12px 14px}
.tiles{display:flex;flex-wrap:wrap;gap:10px}
.tile{border:1px solid var(--line);border-radius:5px;padding:9px 13px;min-width:118px;
background:#0d1117}
.tile .k{color:var(--dim);font-size:10px;letter-spacing:.08em;text-transform:uppercase}
.tile .v{font-size:20px;margin-top:3px}
.tile.stalled .v{color:var(--stalled)}.tile.starved .v{color:var(--starved)}
.tile.flowing .v{color:var(--flowing)}.tile.idle .v{color:var(--idle)}
.tile.unknown .v{color:var(--unknown)}
.scroll{overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:12px}
th{text-align:left;color:var(--dim);font-weight:400;font-size:10px;
letter-spacing:.08em;text-transform:uppercase;padding:5px 9px;
border-bottom:1px solid var(--line);white-space:nowrap}
td{padding:5px 9px;border-bottom:1px solid #21262d;white-space:nowrap}
tbody tr:last-child td{border-bottom:none}
.num{text-align:right;font-variant-numeric:tabular-nums}
.state{font-weight:700;letter-spacing:.05em}
.s-STALLED{color:var(--stalled)}.s-STARVED{color:var(--starved)}
.s-FLOWING{color:var(--flowing)}.s-IDLE{color:var(--idle)}
.s-UNKNOWN{color:var(--unknown)}
.refusal{border-left:3px solid var(--starved);padding:9px 13px;background:#1c1a12}
.refusal .code{color:var(--starved);font-weight:700}
.refusal p{margin:5px 0 0;color:var(--dim);max-width:76ch}
.empty{border-left:3px solid var(--idle);padding:9px 13px;background:#14181d}
.empty .code{color:var(--idle);font-weight:700}
.empty p{margin:5px 0 0;color:var(--dim);max-width:76ch}
.src{color:var(--dim);font-size:10px}
footer{padding:14px 22px;border-top:1px solid var(--line);color:var(--dim);
font-size:11px;max-width:96ch}
"""


def _esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def _ticket_ref(ticket: str) -> str:
    """Render a ticket as literal text, never as a link.

    A hyperlink here would mean a tracker URL literal in the source, which the
    OMN-12818 URL-authority gate correctly rejects: every URL this codebase
    emits must resolve from the routing authority or the integration catalog.
    A ticket id is searchable as text and needs no exemption to be useful.
    """
    return f"<code>{_esc(ticket)}</code>"


def _render_read_status(read: ModelProjectionRead) -> str:
    """Render the honest not-LIVE block for an exposure, or nothing when LIVE."""
    if read.state == EnumPanelState.LIVE:
        return ""
    ticket = (
        f" &middot; tracked by {_ticket_ref(read.migration_ticket)}"
        if read.migration_ticket
        else ""
    )
    css = "refusal" if read.state == EnumPanelState.REFUSED else "empty"
    return (
        f'<div class="{css}"><span class="code">{_esc(read.state)}: '
        f"{_esc(read.reason_code)}</span>{ticket}"
        f"<p>{_esc(read.reason_detail)}</p>"
        f'<p class="src">exposure: {_esc(read.topic)}</p></div>'
    )


def _render_flow(panel: ModelFlowPanel) -> str:
    head = (
        "<h2><span>event flow &mdash; consumer seams</span>"
        f'<span class="src">{_esc(panel.read.topic)} &middot; '
        f"latest window {_esc(panel.read.latest_event_at or 'n/a')}</span></h2>"
    )
    if panel.read.state != EnumPanelState.LIVE:
        return f'<section>{head}<div class="body">{_render_read_status(panel.read)}</div></section>'

    tiles = [
        f'<div class="tile"><div class="k">seams</div>'
        f'<div class="v">{panel.consumer_count}</div></div>'
    ]
    tiles += [
        f'<div class="tile {_esc(state.lower())}"><div class="k">{_esc(state)}</div>'
        f'<div class="v">{count}</div></div>'
        for state, count in panel.state_counts
    ]
    tiles.append(
        f'<div class="tile{" stalled" if panel.dlq_total else ""}">'
        f'<div class="k">dlq depth</div><div class="v">{panel.dlq_total}</div></div>'
    )
    tiles.append(
        f'<div class="tile{" stalled" if panel.handler_error_total else ""}">'
        f'<div class="k">handler errors</div>'
        f'<div class="v">{panel.handler_error_total}</div></div>'
    )

    if panel.attention:
        rows = "".join(
            "<tr>"
            f'<td class="state s-{_esc(c.flow_state)}">{_esc(c.flow_state)}</td>'
            f"<td>{_esc(c.consumer_group)}</td>"
            f"<td>{_esc(c.topic)}</td>"
            f'<td class="num">{c.messages_in}</td>'
            f'<td class="num">{c.messages_out}</td>'
            f'<td class="num">{c.messages_dlq}</td>'
            f'<td class="num">{c.handler_errors}</td>'
            f"<td>{_esc(c.upstream_evidence)}</td>"
            f"<td>{_esc(c.window_end)}</td>"
            "</tr>"
            for c in panel.attention
        )
        table = (
            '<div class="scroll"><table><thead><tr>'
            "<th>state</th><th>consumer group</th><th>topic</th>"
            '<th class="num">in</th><th class="num">out</th>'
            '<th class="num">dlq</th><th class="num">errors</th>'
            "<th>upstream</th><th>window end</th>"
            "</tr></thead><tbody>"
            f"{rows}</tbody></table></div>"
            f'<p class="src">{panel.idle_count} IDLE seam(s) not listed &mdash; '
            "IDLE means nothing was produced upstream either, which is quiet and "
            "correct, not a fault.</p>"
        )
    else:
        table = (
            '<div class="empty"><span class="code">no seam needs attention</span>'
            f"<p>all {panel.idle_count} observed seam(s) reported IDLE in their "
            "latest window: zero in, zero out, and nothing producing upstream.</p>"
            "</div>"
        )

    return (
        f"<section>{head}"
        f'<div class="body"><div class="tiles">{"".join(tiles)}</div></div>'
        f'<div class="body">{table}</div></section>'
    )


def _render_savings(panel: ModelSavingsPanel) -> str:
    head = (
        "<h2><span>delegation savings &mdash; local vs cloud</span>"
        '<span class="src">local-vs-cloud cost from the delegation '
        "projections</span></h2>"
    )
    if panel.has_data:
        tiles = "".join(
            f'<div class="tile"><div class="k">{_esc(m.label)}</div>'
            f'<div class="v">{_esc(m.value)}</div>'
            f'<div class="src">{_esc(m.source_topic)}</div></div>'
            for m in panel.metrics
        )
        body = f'<div class="tiles">{tiles}</div>'
    else:
        body = (
            '<div class="refusal"><span class="code">NO DELEGATION SAVINGS DATA'
            "</span>"
            "<p>Every delegation-savings exposure below refused or is empty. "
            "This panel is wired to those exposures and renders nothing rather "
            "than a zero: a rendered zero-dollar total here would be "
            "indistinguishable from a measured result. Root cause is tracked by "
            f"{_ticket_ref(_TICKET_DELEGATION_ROWS)} "
            "(delegation_events has never held a row; the inference-response "
            "projection is RLS-rejected) behind "
            f"{_ticket_ref(_TICKET_NOT_BUS_BACKED)} (these exposures have not "
            "converted to the bus-fed serving path).</p></div>"
        )
    statuses = "".join(_render_read_status(read) for read in panel.reads)
    return f'<section>{head}<div class="body">{body}</div><div class="body">{statuses}</div></section>'


def _render_rows_panel(read: ModelProjectionRead, title: str, note: str) -> str:
    head = (
        f"<h2><span>{_esc(title)}</span>"
        f'<span class="src">{_esc(read.topic)}</span></h2>'
    )
    if read.state != EnumPanelState.LIVE:
        return (
            f"<section>{head}"
            f'<div class="body"><p class="src">{_esc(note)}</p>'
            f"{_render_read_status(read)}</div></section>"
        )
    columns = list(read.rows[0].keys())
    header = "".join(f"<th>{_esc(col)}</th>" for col in columns)
    body = "".join(
        "<tr>"
        + "".join(f"<td>{_esc(row.get(col, ''))}</td>" for col in columns)
        + "</tr>"
        for row in read.rows
    )
    return (
        f"<section>{head}"
        f'<div class="body"><p class="src">{_esc(note)} &middot; '
        f"{len(read.rows)} of {read.cached_row_count} cached row(s) &middot; "
        f"latest {_esc(read.latest_event_at or 'n/a')}</p>"
        f'<div class="scroll"><table><thead><tr>{header}</tr></thead>'
        f"<tbody>{body}</tbody></table></div></div></section>"
    )


def _render_inventory(rows: tuple[ModelInventoryRow, ...], bus_backed: int) -> str:
    body = "".join(
        "<tr>"
        f"<td>{_esc(row.backing)}</td>"
        f"<td>{_esc(row.topic)}</td>"
        f"<td>{_esc(row.source_contract)}</td>"
        f'<td class="num">{row.cached_row_count if row.backing == "bus" else "&mdash;"}</td>'
        f"<td>{_esc(row.latest_event_at or '—')}</td>"
        f"<td>{'yes' if row.tenant_scoped else ''}</td>"
        "</tr>"
        for row in rows
    )
    return (
        "<section><h2><span>projection exposure census</span>"
        f'<span class="src">{bus_backed} of {len(rows)} bus-backed</span></h2>'
        '<div class="body"><p class="src">Every exposure this process discovered '
        "from contracts. A <code>not_yet_bus_backed</code> exposure answers 503 on "
        "the API and renders a refusal here &mdash; it is not an empty dataset.</p>"
        '<div class="scroll"><table><thead><tr>'
        "<th>backing</th><th>exposure</th><th>source contract</th>"
        '<th class="num">cached rows</th><th>latest event</th><th>tenant-scoped</th>'
        f"</tr></thead><tbody>{body}</tbody></table></div></div></section>"
    )


def page_title(service_name: str) -> str:
    """The standing document title, lane included (OMN-17346).

    The lane is the serving process's own service identity — not a caller-
    supplied label and not a guess from the request Host — so a page cannot
    claim to be a lane it is not.
    """
    return f"{PAGE_NAME} — {service_name}"


def render_morning_page(page: ModelMorningPage) -> str:
    """Render the assembled page model to a single self-contained HTML document."""
    sections = [
        _render_flow(page.flow),
        _render_savings(page.savings),
        _render_rows_panel(
            page.registry,
            "node registry — runtime services",
            "which nodes registered and what health they last reported",
        ),
        _render_rows_panel(
            page.live_events,
            "live events",
            "the most recent platform events on the live-events exposure",
        ),
        _render_rows_panel(
            page.work_events,
            "work events",
            "what actors actually did — the L1 work-ledger rung of OMN-16176, "
            "one content-addressed row per session event",
        ),
        _render_rows_panel(
            page.sessions,
            "sessions",
            "session replay snapshots",
        ),
        _render_rows_panel(
            page.skill_executions,
            "tool / skill executions",
            "skill and tool execution snapshots",
        ),
        _render_inventory(page.inventory, page.bus_backed_count),
    ]
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<meta http-equiv="refresh" content="{page.refresh_seconds}">'
        f"<title>{_esc(page_title(page.service_name))}</title>"
        f"<style>{_STYLE}</style></head><body>"
        f"<header><h1>{_esc(PAGE_NAME)}</h1>"
        f'<span class="lane">{_esc(page.service_name)}</span>'
        f'<span class="asof">as of {_esc(page.generated_at)}</span>'
        f'<span class="meta">auto-refresh {page.refresh_seconds}s</span>'
        f'<span class="meta">{page.bus_backed_count}/{page.exposure_count} '
        "exposures bus-backed</span></header>"
        f"<main>{''.join(sections)}</main>"
        "<footer>Server-rendered from this process&rsquo;s in-memory snapshot "
        "cache, which is fed by the compacted "
        "<code>onex.snapshot.projection.*</code> topics. No database handle, no "
        "client-side fetch, no client-side derivation: every verdict on this "
        "page was written by the reducer that owns it. A panel that could not be "
        "served says so and names its ticket &mdash; it never renders a zero it "
        f"did not measure. {_ticket_ref(_TICKET_PAGE)} "
        f"{_ticket_ref(_TICKET_ALWAYS_ON)}</footer></body></html>"
    )


__all__ = [
    "DEFAULT_REFRESH_SECONDS",
    "PAGE_NAME",
    "EnumPanelState",
    "ModelFlowConsumer",
    "ModelFlowPanel",
    "ModelInventoryRow",
    "ModelMorningPage",
    "ModelProjectionRead",
    "ModelSavingsMetric",
    "ModelSavingsPanel",
    "build_flow_panel",
    "build_inventory",
    "build_morning_page",
    "build_savings_panel",
    "latest_window_per_consumer",
    "page_title",
    "read_projection",
    "render_morning_page",
]
