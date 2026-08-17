# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Delegation-routing ROI overlay — the first closed platform learning loop (OMN-14001).

Captured outcomes in the ``context_roi_scores`` projection table (materialised by
``node_projection_context_roi`` from the context-ROI runner terminal) are read
back here and turned into a deterministic, in-memory *ROI overlay* that the pure
routing reducer consults BEFORE the static ``routing_tiers.yaml`` order. A tier
whose captured per-tier success rate is below a defensible floor (over a minimum
sample count) is *ROI-suppressed*: the reducer skips it and routes to the next
eligible tier. This is the read that closes the loop — until now nothing read a
stored outcome back into an automated routing decision.

Architecture (why the read lives here, not in ``delta``):
    The routing reducer (``node_delegation_routing_reducer``) is a REDUCER — a
    pure, deterministic function with read-only *config* I/O only
    (``requires_network: false``). A live projection-DB read inside ``delta`` would
    break fresh-process/live parity (the exact divergence OMN-12974 flags) and
    golden-chain replay. So the split is:

      * ``build_roi_overlay`` / ``resolve_roi_overlay`` (this module) — the I/O
        boundary. ``resolve_roi_overlay`` reads ``context_roi_scores`` through the
        ``DatabaseAdapter`` protocol and returns a resolved, in-memory overlay.
      * ``delta`` / ``first_eligible_tier`` / ``next_eligible_tier`` (the reducer)
        — accept the overlay as a pure INPUT (``roi_overlay=None`` by default, in
        which case behaviour is byte-identical to before this change, so every
        existing golden replay is untouched).

    The caller on the live path (the local delegation dispatch port, or a bus
    orchestrator effect) reads the overlay and threads it in. ``resolve_roi_overlay``
    is fail-OPEN for TELEMETRY OUTAGES: a read error / missing table / unreachable
    DB returns ``None``, so an outage degrades to the static tier order and NEVER
    breaks routing.

Tenant seam (OMN-16092) — the ONE fail-CLOSED exception to that posture:
    ``context_roi_scores`` is RLS-covered (migration
    ``003_context_roi_scores_tenant_id_and_rls.sql``, policy ``tenant_isolation``).
    With ``app.tenant_id`` unset the policy predicate is NULL and the read returns
    ZERO ROWS rather than erroring — so a blinded read looked EXACTLY like "no ROI
    data yet" and silently degraded this overlay to static routing with no error,
    no 5xx and no distinguishing signal. That is a correctness defect, not an
    outage, so ``TenantContextMissingError`` is deliberately NOT absorbed by the
    fail-open handlers below: it propagates to the caller, which must fail closed
    rather than route on a silently-blinded read. The seam itself lives in
    ``PostgresReadDatabaseAdapter`` (``set_config('app.tenant_id', ...)`` before
    every SELECT), and ``resolve_context_roi_db`` resolves the tenant eagerly so
    the refusal surfaces at wiring time rather than mid-decision.

The ``context_roi_scores`` table carries no ``task_type`` column; its rows are the
context-ROI experiment's per-(task x arm x model) generation outcomes. Each row's
``endpoint_ref`` (a routing-tier backend id, e.g. ``local-coder``) maps to a tier
via the routing authority (``tier_for_backend``, injected here as
``tier_of_endpoint`` to keep this module free of a reducer import). We therefore
aggregate the observed generation success rate PER TIER and apply it to the
requested ``task_type``. This is the honest first-slice signal: "this tier
empirically fails the generation work we measure, so stop starting there." The
per-task_type refinement is tracked as follow-up.

Bounded-recency window (OMN-14019):
    By default the aggregation reads the WHOLE table's lifetime per-tier success
    rate. That encodes a latent regression-blindness bug: a tier that accumulates
    enough historical successes can never be suppressed afterward even if it later
    regresses to failing, because lifetime successes permanently dominate. ROI should reflect
    RECENT performance. Two env-tunable knobs scope the aggregation to a recent
    cohort per tier:

      * ``DELEGATION_ROI_WINDOW_SECONDS`` — only rows whose ``created_at`` is within
        this many seconds of "now" are counted.
      * ``DELEGATION_ROI_LOOKBACK_ROWS`` — only the most-recent K rows per tier (by
        ``created_at``) are counted.

    BOTH default to unset, in which case the aggregation is byte-identical to the
    original whole-table behaviour, so every existing golden-chain replay is
    untouched — only an opt-in surface (e.g. the stability runtime) enables a
    window. When a window IS active, a row without a parseable ``created_at``
    cannot be placed on the recency axis and is excluded from that tier's cohort
    (the real Postgres column is ``NOT NULL DEFAULT NOW()``, so this only affects
    synthetic/legacy rows). See OMN-14011 §5/§7 for the design.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.projection.tenant_isolation import (
    TenantContextMissingError,
    resolve_rls_read_tenant,
)

if TYPE_CHECKING:
    from omnimarket.projection.protocol_database import DatabaseAdapter

_logger = logging.getLogger(__name__)

#: Projection table read back into the routing decision (OMN-14001).
CONTEXT_ROI_TABLE = "context_roi_scores"

#: Minimum captured samples for a tier before its ROI signal is allowed to
#: suppress routing. Below this, the signal is treated as too thin to act on and
#: the tier is never suppressed (static order wins). Env-overridable.
DEFAULT_ROI_MIN_SAMPLES = 5

#: Success-rate floor. A tier with >= MIN_SAMPLES captured rows whose observed
#: ``final_success`` rate is strictly below this floor is ROI-suppressed.
#: Env-overridable. 0.5 == "empirically fails more than half the time".
DEFAULT_ROI_SUCCESS_FLOOR = 0.5

_ENV_MIN_SAMPLES = "DELEGATION_ROI_MIN_SAMPLES"
_ENV_SUCCESS_FLOOR = "DELEGATION_ROI_SUCCESS_FLOOR"

#: Bounded-recency knobs (OMN-14019). BOTH default to unset (``None``), which
#: preserves the original whole-table/lifetime aggregation byte-for-byte so
#: golden replays are untouched. Set on an opt-in surface to make the ROI signal
#: reflect RECENT performance and let a regressed tier be suppressed.
_ENV_LOOKBACK_ROWS = "DELEGATION_ROI_LOOKBACK_ROWS"
_ENV_WINDOW_SECONDS = "DELEGATION_ROI_WINDOW_SECONDS"

#: DSN env var for the projection DB that materialises ``context_roi_scores``.
#: This is the ``omnidash_analytics`` database (the context-ROI projection sink),
#: NOT the local SQLite ``delegation_events`` evidence DB. The canonical name is
#: reused from the projection tooling (node_projection_llm_cost / node_retention_cleanup).
_ENV_CONTEXT_ROI_DSN = "OMNIDASH_ANALYTICS_DB_URL"


def resolve_roi_min_samples() -> int:
    """Return the min-sample gate, honouring ``DELEGATION_ROI_MIN_SAMPLES``.

    A malformed or non-positive override falls back to the default rather than
    silently disabling the sample gate (which would let a single unlucky row
    suppress a tier).
    """
    raw = os.environ.get(_ENV_MIN_SAMPLES)
    if raw is None:
        return DEFAULT_ROI_MIN_SAMPLES
    try:
        value = int(raw)
    except ValueError:
        _logger.warning(
            "invalid %s=%r; using default %d",
            _ENV_MIN_SAMPLES,
            raw,
            DEFAULT_ROI_MIN_SAMPLES,
        )
        return DEFAULT_ROI_MIN_SAMPLES
    return value if value >= 1 else DEFAULT_ROI_MIN_SAMPLES


def resolve_roi_success_floor() -> float:
    """Return the success-rate floor, honouring ``DELEGATION_ROI_SUCCESS_FLOOR``.

    A malformed or out-of-range (not in [0, 1]) override falls back to the
    default. A floor of 0.0 would suppress nothing; 1.0 would suppress any tier
    with a single failure — both are permitted only if explicitly and validly set.
    """
    raw = os.environ.get(_ENV_SUCCESS_FLOOR)
    if raw is None:
        return DEFAULT_ROI_SUCCESS_FLOOR
    try:
        value = float(raw)
    except ValueError:
        _logger.warning(
            "invalid %s=%r; using default %.3f",
            _ENV_SUCCESS_FLOOR,
            raw,
            DEFAULT_ROI_SUCCESS_FLOOR,
        )
        return DEFAULT_ROI_SUCCESS_FLOOR
    return value if 0.0 <= value <= 1.0 else DEFAULT_ROI_SUCCESS_FLOOR


def resolve_roi_lookback_rows() -> int | None:
    """Return the per-tier last-K recency cap, honouring ``DELEGATION_ROI_LOOKBACK_ROWS``.

    ``None`` when the env var is unset OR malformed OR non-positive — i.e. the
    lookback cap is disabled and the whole-table (lifetime) cohort is used. A
    non-positive value is treated as "unset" rather than "keep zero rows", so a
    typo can never silently blind every tier.
    """
    raw = os.environ.get(_ENV_LOOKBACK_ROWS)
    if raw is None:
        return None
    try:
        value = int(raw)
    except ValueError:
        _logger.warning(
            "invalid %s=%r; ignoring (lifetime cohort)", _ENV_LOOKBACK_ROWS, raw
        )
        return None
    return value if value >= 1 else None


def resolve_roi_window_seconds() -> float | None:
    """Return the recency time window, honouring ``DELEGATION_ROI_WINDOW_SECONDS``.

    ``None`` when unset OR malformed OR non-positive — the time window is disabled
    and the whole-table (lifetime) cohort is used. A non-positive value is treated
    as "unset" rather than "keep zero rows".
    """
    raw = os.environ.get(_ENV_WINDOW_SECONDS)
    if raw is None:
        return None
    try:
        value = float(raw)
    except ValueError:
        _logger.warning(
            "invalid %s=%r; ignoring (lifetime cohort)", _ENV_WINDOW_SECONDS, raw
        )
        return None
    return value if value > 0.0 else None


class ModelTierRoiSignal(BaseModel):
    """Aggregated captured ROI for a single routing tier.

    ``success_rate`` is the fraction of captured ``context_roi_scores`` rows for
    the tier whose ``final_success`` was True. ``suppressed`` records the gate
    verdict (>= min_samples AND success_rate < floor) so the decision is
    self-describing in logs / evidence.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    tier_name: str = Field(min_length=1)
    sample_count: int = Field(ge=0)
    success_count: int = Field(ge=0)
    success_rate: float = Field(ge=0.0, le=1.0)
    suppressed: bool = Field(default=False)


class ModelRoutingRoiOverlay(BaseModel):
    """Resolved, in-memory ROI signal threaded into the pure routing reducer.

    Built from ``context_roi_scores`` at the I/O boundary and passed as a pure
    input to ``delta`` / ``first_eligible_tier`` / ``next_eligible_tier``. Carries
    the per-tier signals plus the resolved gate thresholds so the routing decision
    is fully attributable to captured evidence (proof_class: read-back).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_type: str = Field(min_length=1)
    min_samples: int = Field(ge=1)
    success_floor: float = Field(ge=0.0, le=1.0)
    signals: tuple[ModelTierRoiSignal, ...] = Field(default_factory=tuple)

    @property
    def suppressed_tiers(self) -> frozenset[str]:
        """Tier names whose captured ROI crossed the suppression gate."""
        return frozenset(s.tier_name for s in self.signals if s.suppressed)

    def is_suppressed(self, tier_name: str) -> bool:
        """Whether ``tier_name`` is ROI-suppressed by captured evidence."""
        return tier_name in self.suppressed_tiers


def _row_success(row: dict[str, object]) -> bool:
    """Interpret a ``context_roi_scores`` row's ``final_success`` flag.

    Handles the bool the in-memory/SQLite adapters return and the ``0/1`` /
    ``'t'``/``'f'`` an asyncpg/psql read can surface, so the aggregation is
    adapter-agnostic.
    """
    value = row.get("final_success")
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"t", "true", "1", "yes"}
    return False


def _row_created_at(row: dict[str, object]) -> datetime | None:
    """Interpret a ``context_roi_scores`` row's ``created_at`` as tz-aware UTC.

    Handles the ``datetime`` a psycopg2 ``TIMESTAMPTZ`` read returns and the ISO
    string an in-memory/JSON fixture may carry (a trailing ``Z`` is accepted).
    A naive datetime is assumed UTC so recency comparisons never mix aware/naive.
    Returns ``None`` when the value is absent or unparseable — such a row cannot
    be placed on the recency axis.
    """
    value = row.get("created_at")
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        if raw.endswith(("Z", "z")):
            raw = raw[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            return None
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    return None


def _row_sort_key(row: dict[str, object]) -> str:
    """Stable per-row tiebreak for equal ``created_at`` values (determinism).

    Uses the table's unique ``correlation_id`` (falling back to ``id``) so two rows
    with an identical timestamp at the last-K boundary always resolve the same way.
    """
    for key in ("correlation_id", "id"):
        value = row.get(key)
        if value is not None:
            return str(value)
    return ""


def _apply_recency_window(
    tier_rows: dict[str, list[dict[str, object]]],
    *,
    lookback_rows: int | None,
    window_seconds: float | None,
    now: datetime | None,
) -> dict[str, list[dict[str, object]]]:
    """Scope each tier's rows to a recent cohort (OMN-14019).

    Applies, per tier, in order: (1) a time-window filter keeping only rows whose
    ``created_at`` is within ``window_seconds`` of ``now``; (2) a last-K cap keeping
    only the most-recent ``lookback_rows`` by ``created_at``. Rows without a
    parseable ``created_at`` are dropped (they cannot be ranked by recency). The
    result is deterministic: rows are ordered by ``(created_at, _row_sort_key)``.

    Caller guarantees at least one of ``lookback_rows`` / ``window_seconds`` is set
    (the whole-table path never calls this, keeping that path byte-identical).
    """
    cutoff: datetime | None = None
    if window_seconds is not None:
        reference_now = now if now is not None else datetime.now(tz=UTC)
        cutoff = reference_now - timedelta(seconds=window_seconds)

    windowed: dict[str, list[dict[str, object]]] = {}
    for tier, rows in tier_rows.items():
        ranked: list[tuple[datetime, str, dict[str, object]]] = []
        for row in rows:
            created = _row_created_at(row)
            if created is None:
                continue
            if cutoff is not None and created < cutoff:
                continue
            ranked.append((created, _row_sort_key(row), row))
        ranked.sort(key=lambda item: (item[0], item[1]))
        kept = [item[2] for item in ranked]
        if lookback_rows is not None:
            kept = kept[-lookback_rows:]
        if kept:
            windowed[tier] = kept
    return windowed


def build_roi_overlay(
    rows: list[dict[str, object]],
    *,
    task_type: str,
    tier_of_endpoint: Callable[[str], str | None],
    min_samples: int | None = None,
    success_floor: float | None = None,
    lookback_rows: int | None = None,
    window_seconds: float | None = None,
    now: datetime | None = None,
) -> ModelRoutingRoiOverlay:
    """Aggregate ``context_roi_scores`` rows into a deterministic ROI overlay.

    Rows are grouped by the routing tier their ``endpoint_ref`` maps to (via the
    injected ``tier_of_endpoint`` — the routing authority's ``tier_for_backend``).
    Rows whose endpoint does not map to a known tier are ignored (they cannot
    inform a tier decision). A tier is suppressed when it has ``>= min_samples``
    rows AND its ``final_success`` rate is strictly below ``success_floor``.

    Bounded recency (OMN-14019): when ``lookback_rows`` and/or ``window_seconds``
    resolve to a value (explicit arg, else the ``DELEGATION_ROI_LOOKBACK_ROWS`` /
    ``DELEGATION_ROI_WINDOW_SECONDS`` env vars), each tier's cohort is first scoped
    to its most-recent rows before the success rate is computed, so a tier that has
    regressed can be suppressed despite a large lifetime success history. When
    BOTH are unset (the default), the aggregation is byte-identical to the original
    whole-table behaviour — the recency code is never entered — so golden replays
    are untouched. ``now`` overrides the window reference clock (tests only).

    Pure and deterministic: signals are emitted in sorted tier-name order so the
    same rows always yield the same overlay.
    """
    resolved_min = min_samples if min_samples is not None else resolve_roi_min_samples()
    resolved_floor = (
        success_floor if success_floor is not None else resolve_roi_success_floor()
    )
    resolved_lookback = (
        lookback_rows if lookback_rows is not None else resolve_roi_lookback_rows()
    )
    resolved_window = (
        window_seconds if window_seconds is not None else resolve_roi_window_seconds()
    )

    counts: dict[str, list[int]] = {}  # tier -> [success_count, sample_count]
    if resolved_lookback is None and resolved_window is None:
        # Whole-table / lifetime path — byte-identical to pre-OMN-14019 behaviour.
        for row in rows:
            endpoint = row.get("endpoint_ref")
            if not isinstance(endpoint, str) or not endpoint:
                continue
            tier = tier_of_endpoint(endpoint)
            if not tier:
                continue
            bucket = counts.setdefault(tier, [0, 0])
            bucket[1] += 1
            if _row_success(row):
                bucket[0] += 1
    else:
        # Bounded-recency path — group rows per tier, then scope to a recent cohort.
        tier_rows: dict[str, list[dict[str, object]]] = {}
        for row in rows:
            endpoint = row.get("endpoint_ref")
            if not isinstance(endpoint, str) or not endpoint:
                continue
            tier = tier_of_endpoint(endpoint)
            if not tier:
                continue
            tier_rows.setdefault(tier, []).append(row)
        windowed = _apply_recency_window(
            tier_rows,
            lookback_rows=resolved_lookback,
            window_seconds=resolved_window,
            now=now,
        )
        for tier, tier_row_list in windowed.items():
            success_count = sum(1 for row in tier_row_list if _row_success(row))
            counts[tier] = [success_count, len(tier_row_list)]

    signals: list[ModelTierRoiSignal] = []
    for tier in sorted(counts):
        success_count, sample_count = counts[tier]
        success_rate = (success_count / sample_count) if sample_count else 0.0
        suppressed = sample_count >= resolved_min and success_rate < resolved_floor
        signals.append(
            ModelTierRoiSignal(
                tier_name=tier,
                sample_count=sample_count,
                success_count=success_count,
                success_rate=success_rate,
                suppressed=suppressed,
            )
        )

    return ModelRoutingRoiOverlay(
        task_type=task_type,
        min_samples=resolved_min,
        success_floor=resolved_floor,
        signals=tuple(signals),
    )


def resolve_roi_overlay(
    db: DatabaseAdapter,
    *,
    task_type: str,
    tier_of_endpoint: Callable[[str], str | None],
    min_samples: int | None = None,
    success_floor: float | None = None,
    lookback_rows: int | None = None,
    window_seconds: float | None = None,
) -> ModelRoutingRoiOverlay | None:
    """Read ``context_roi_scores`` and build the ROI overlay — FAIL-OPEN on outage.

    Reads every captured row through the ``DatabaseAdapter.query`` protocol
    (in-memory / SQLite / asyncpg all satisfy it) and aggregates via
    ``build_roi_overlay``. Returns ``None`` on a read FAILURE — missing table,
    unreachable DB, adapter error — so a telemetry outage degrades to the static
    tier order and never breaks a live routing decision. An empty (but readable)
    table returns an overlay with no signals, which the reducer treats exactly
    like the static path.

    OMN-16092 — ``TenantContextMissingError`` is the ONE exception and PROPAGATES:
    ``context_roi_scores`` is RLS-covered, so a read with no ``app.tenant_id``
    tenant context returns zero rows instead of erroring. Swallowing that into a
    ``None`` (or into an empty overlay) would be exactly the silent static-routing
    degrade this ticket exists to remove — the caller must fail closed on it.

    ``lookback_rows`` / ``window_seconds`` (else the ``DELEGATION_ROI_LOOKBACK_ROWS``
    / ``DELEGATION_ROI_WINDOW_SECONDS`` env vars, resolved inside ``build_roi_overlay``)
    scope the aggregation to a recent per-tier cohort (OMN-14019). Both unset keeps
    the whole-table behaviour. The full table is still read here — the window is a
    pure in-memory scope over the read rows, so the read path and its fail-open
    guarantee are unchanged.
    """
    try:
        rows = db.query(CONTEXT_ROI_TABLE)
    except TenantContextMissingError:
        # FAIL-CLOSED (OMN-16092): never degrade to static routing on a missing
        # tenant seam — an RLS-blinded read is indistinguishable from an empty
        # table, so absorbing it here is precisely the silent degrade the
        # fail-open handler below must not be allowed to cover.
        _logger.error(
            "roi_overlay read refused for task_type=%s: no tenant context for "
            "RLS-covered %s; failing closed rather than routing on a blinded read",
            task_type,
            CONTEXT_ROI_TABLE,
        )
        raise
    except Exception:
        _logger.warning(
            "roi_overlay read failed for task_type=%s; falling back to static tiers",
            task_type,
            exc_info=True,
        )
        return None

    overlay = build_roi_overlay(
        rows,
        task_type=task_type,
        tier_of_endpoint=tier_of_endpoint,
        min_samples=min_samples,
        success_floor=success_floor,
        lookback_rows=lookback_rows,
        window_seconds=window_seconds,
    )
    if overlay.suppressed_tiers:
        _logger.info(
            "roi_overlay resolved for task_type=%s: suppressed_tiers=%s "
            "(min_samples=%d floor=%.3f, %d tier signals)",
            task_type,
            sorted(overlay.suppressed_tiers),
            overlay.min_samples,
            overlay.success_floor,
            len(overlay.signals),
        )
    return overlay


def resolve_context_roi_db() -> DatabaseAdapter | None:
    """Resolve a read-only projection adapter for ``context_roi_scores``.

    Gated on the ``OMNIDASH_ANALYTICS_DB_URL`` DSN (the projection DB that
    materialises the table). Returns ``None`` when the DSN is unset (the common
    local case — no ROI read, static routing) or on a construction error, so a
    delegation never fails because a telemetry DB is absent or unreachable. The
    adapter connects LAZILY with a bounded timeout, so this factory does no I/O —
    the first actual read happens inside ``resolve_roi_overlay``.

    This is the live-wiring entrypoint (OMN-14001): the delegation dispatch-port
    selector calls it to point the ROI reader at the real projection DB. Its
    fail-open posture is a deliberate, documented exception to the
    fail-fast-on-missing-env rule — a telemetry read must degrade to static
    routing, never crash the caller.

    OMN-16092: the tenant the adapter will read under is resolved HERE, eagerly
    (no I/O — a settings read), and injected. That places the fail-closed refusal
    at wiring time, where it is attributable, instead of mid-decision. A
    ``TenantContextMissingError`` therefore PROPAGATES out of this factory rather
    than being absorbed into a ``None`` — returning ``None`` for it would restore
    the exact silent static-routing degrade this fixes.
    """
    dsn = os.environ.get(_ENV_CONTEXT_ROI_DSN, "").strip()
    if not dsn:
        return None
    # Resolved before the import/construction below so the refusal is not
    # reachable through the fail-open handler.
    tenant = resolve_rls_read_tenant(None, table=CONTEXT_ROI_TABLE)
    try:
        from omnimarket.projection.postgres_read_database import (
            PostgresReadDatabaseAdapter,
        )

        return PostgresReadDatabaseAdapter(dsn, tenant_id=tenant)
    except Exception:
        _logger.warning(
            "resolve_context_roi_db failed to construct a projection adapter from "
            "%s; ROI read disabled (static routing)",
            _ENV_CONTEXT_ROI_DSN,
            exc_info=True,
        )
        return None


__all__ = [
    "CONTEXT_ROI_TABLE",
    "DEFAULT_ROI_MIN_SAMPLES",
    "DEFAULT_ROI_SUCCESS_FLOOR",
    "ModelRoutingRoiOverlay",
    "ModelTierRoiSignal",
    "build_roi_overlay",
    "resolve_context_roi_db",
    "resolve_roi_lookback_rows",
    "resolve_roi_min_samples",
    "resolve_roi_overlay",
    "resolve_roi_success_floor",
    "resolve_roi_window_seconds",
]
