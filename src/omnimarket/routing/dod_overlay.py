# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Delegation routing reads the DoD pass rate per (task type, model) (OMN-19528).

The OMN-14001 overlay (``routing.roi_overlay``) is the one seam through which a
stored outcome steers a delegation routing decision: the pure routing reducer
takes a ``ModelRoutingRoiOverlay`` and skips a suppressed tier on a first pass,
with a fail-safe second pass that never lets the overlay dead-end routing. Its
original source, ``context_roi_scores``, holds no rows on the lab, and only the
bus-less local port ever read it.

This module feeds the SAME seam from a different, deterministic outcome: whether
the delegated run's ticket verified its definition of done. OMN-19514 made the
two halves joinable -- ``omninode_internal.dod_verify_runs.delegation_correlation_id``
names the delegation run a verification judged, and ``delegation_events`` carries
that run's task type, tier and model -- so the pass rate per (task type, tier,
model) is one join away.

Split, as in ``roi_overlay``:

* ``build_dod_overlay`` -- the pure fold over joined rows. No I/O, deterministic,
  so it is testable and replayable.
* ``join_delegations_to_verdicts`` -- the pure join of the two reads.
* ``PostgresDodOutcomeReader`` / ``resolve_dod_overlay`` -- the I/O boundary,
  called by the caller that owns it (the deployed-lane routing consumer and the
  local dispatch-port selector), never from inside the reducer.

What counts:

* A sample is a delegation run of the requested task type whose DoD verdict
  DECIDED: status ``verified`` or ``failed``. ``skipped`` (no contract to check),
  ``unresolved`` (the verifier faulted) and ``pending`` say nothing about the
  attempt, so they are not samples at all rather than silent failures.
* A pass is outcome ``done``: the typed done predicate of OMN-18900 (zero failed
  checks and at least one behaviour-proving check). A ``verified`` status that
  proved no behaviour is a fail, as the predicate says.
* One run, one sample: when a run was verified more than once, the latest
  decided verdict wins (ties broken by the verdict's own correlation id).
* The tier signal pools every model of that tier, because the reducer routes by
  tier; the per-model breakdown is carried beside it so the decision log can cite
  the (task type, model) rate it acted on.

Thresholds are the OMN-14001 ones, read through the same resolvers and env vars
(minimum sample, floor, OMN-14019 recency window and lookback), so one knob set
governs both reads.

Tenancy (OMN-16092 posture, unchanged): the read runs under exactly one tenant,
filtered explicitly on ``delegation_events.tenant_id`` AND with ``app.tenant_id``
set for the RLS policy, so it is correct whether or not the table's RLS is
enabled. An unresolvable tenant raises ``TenantContextMissingError`` before any
SQL. Any other read failure returns ``None``, which the reducer treats as the
static tier order: a telemetry outage never breaks routing.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.projection.tenant_isolation import (
    TENANT_GUC,
    TenantContextMissingError,
    UnmappedTenantIdentityError,
    resolve_rls_read_tenant,
    resolve_tenant_uuid,
)
from omnimarket.routing.roi_overlay import (
    ModelRoutingRoiOverlay,
    ModelTierRoiSignal,
    apply_recency_window,
    resolve_roi_lookback_rows,
    resolve_roi_min_samples,
    resolve_roi_success_floor,
    resolve_roi_window_seconds,
)

if TYPE_CHECKING:
    import psycopg2  # type: ignore[import-untyped]

_logger = logging.getLogger(__name__)

#: Verdict statuses that decided the run. Every other status is not a sample.
DOD_DECIDED_STATUSES: frozenset[str] = frozenset({"verified", "failed"})

#: The only outcome that counts as a pass (``EnumDodEvalOutcome.DONE``).
DOD_DONE_OUTCOME = "done"

#: Default relations the reader joins. Both are schema-qualified because the
#: verdict table lives in ``omninode_internal``, not ``public``.
DELEGATION_EVENTS_RELATION = "public.delegation_events"
DOD_VERIFY_RUNS_RELATION = "omninode_internal.dod_verify_runs"

#: The table named on a tenant refusal (the RLS-covered side of the join).
_TENANT_TABLE = "delegation_events"

#: Same DSN the tenant overlay and the context-ROI read use: the projection DB,
#: whose role reads ``public.delegation_events``.
_ENV_DSN = "OMNIDASH_ANALYTICS_DB_URL"

#: The lane's internal DSN (principal ``omninode_runtime``), the one role
#: granted SELECT on ``omninode_internal.dod_verify_runs`` (migration 0001).
_ENV_VERDICT_DSN = "OMNINODE_INTERNAL_DB_URL"

_CONNECT_TIMEOUT_SECONDS = 3

_RELATION_PATTERN = re.compile(r"^[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*$")

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class ModelDodModelSignal(BaseModel):
    """The DoD pass rate of one model in one tier, for the overlay's task type."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tier_name: str = Field(min_length=1)
    model_name: str = Field(min_length=1)
    sample_count: int = Field(ge=0)
    pass_count: int = Field(ge=0)
    pass_rate: float = Field(ge=0.0, le=1.0)


class ModelRoutingDodOverlay(BaseModel):
    """The DoD read for one task type: the reducer's overlay plus its evidence.

    ``roi_overlay`` is what the routing reducer consumes, unchanged in type, so
    the reducer needs no new input. ``model_signals`` is the per-(tier, model)
    breakdown the tier signals were pooled from; it exists so the decision log
    can cite the rate a routing decision acted on.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_type: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    roi_overlay: ModelRoutingRoiOverlay
    model_signals: tuple[ModelDodModelSignal, ...] = Field(default_factory=tuple)
    joined_verdict_count: int = Field(
        default=0,
        ge=0,
        description="Verdicts joined to a run of this task type, decided or not.",
    )
    undecided_verdict_count: int = Field(
        default=0,
        ge=0,
        description="Of those, verdicts that decided nothing (skipped, "
        "unresolved, pending), so the log shows why a rate has no samples.",
    )

    def describe(self) -> str:
        """One log-friendly clause naming every (tier, model) rate and the gate."""
        if self.model_signals:
            parts = "; ".join(
                f"{s.tier_name}/{s.model_name} n={s.sample_count} "
                f"pass={s.pass_count} rate={s.pass_rate:.3f}"
                for s in self.model_signals
            )
        else:
            parts = "no decided DoD verdicts joined to this task type"
        return (
            f"dod_pass_rate[{parts}] "
            f"joined_verdicts={self.joined_verdict_count} "
            f"undecided={self.undecided_verdict_count} "
            f"suppressed_tiers={sorted(self.roi_overlay.suppressed_tiers)} "
            f"min_samples={self.roi_overlay.min_samples} "
            f"floor={self.roi_overlay.success_floor:.3f}"
        )


def _as_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if isinstance(value, str) and value.strip():
        raw = value.strip()
        if raw.endswith(("Z", "z")):
            raw = raw[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            return None
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    return None


def _as_text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _latest_decided_verdict_per_run(
    rows: list[dict[str, object]], *, task_type: str
) -> list[dict[str, object]]:
    """One row per delegation run: its latest DECIDED verdict for ``task_type``."""
    latest: dict[str, tuple[datetime, str, dict[str, object]]] = {}
    for row in rows:
        if _as_text(row.get("task_type")) != task_type:
            continue
        if _as_text(row.get("verdict_status")) not in DOD_DECIDED_STATUSES:
            continue
        run_id = str(row.get("correlation_id") or "").strip()
        if not run_id:
            continue
        rank = (
            _as_datetime(row.get("verdict_completed_at")) or _EPOCH,
            str(row.get("verdict_correlation_id") or ""),
        )
        held = latest.get(run_id)
        if held is None or rank > (held[0], held[1]):
            latest[run_id] = (rank[0], rank[1], row)
    return [latest[run_id][2] for run_id in sorted(latest)]


def build_dod_overlay(
    rows: list[dict[str, object]],
    *,
    task_type: str,
    tenant_id: str,
    min_samples: int | None = None,
    success_floor: float | None = None,
    lookback_rows: int | None = None,
    window_seconds: float | None = None,
    now: datetime | None = None,
) -> ModelRoutingDodOverlay:
    """Fold joined delegation x verdict rows into the DoD routing overlay.

    Pure and deterministic: the same rows in any order give an equal overlay.
    Rows carry ``task_type``, ``correlation_id`` (the delegation run),
    ``tier_name``, ``model_name``, ``created_at`` (when the run happened, the
    recency axis), ``verdict_correlation_id``, ``verdict_status``,
    ``verdict_outcome`` and ``verdict_completed_at``.
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

    tier_rows: dict[str, list[dict[str, object]]] = {}
    for row in _latest_decided_verdict_per_run(rows, task_type=task_type):
        tier = _as_text(row.get("tier_name"))
        model = _as_text(row.get("model_name"))
        if not tier or not model:
            continue
        tier_rows.setdefault(tier, []).append(row)

    if resolved_lookback is not None or resolved_window is not None:
        tier_rows = apply_recency_window(
            tier_rows,
            lookback_rows=resolved_lookback,
            window_seconds=resolved_window,
            now=now,
        )

    per_model: dict[tuple[str, str], list[int]] = {}  # -> [pass_count, sample_count]
    for tier, cohort in tier_rows.items():
        for row in cohort:
            bucket = per_model.setdefault(
                (tier, _as_text(row.get("model_name"))), [0, 0]
            )
            bucket[1] += 1
            if _as_text(row.get("verdict_outcome")) == DOD_DONE_OUTCOME:
                bucket[0] += 1

    model_signals = tuple(
        ModelDodModelSignal(
            tier_name=tier,
            model_name=model,
            sample_count=samples,
            pass_count=passes,
            pass_rate=passes / samples,
        )
        for (tier, model), (passes, samples) in sorted(per_model.items())
    )

    tier_signals: list[ModelTierRoiSignal] = []
    for tier in sorted({s.tier_name for s in model_signals}):
        samples = sum(s.sample_count for s in model_signals if s.tier_name == tier)
        passes = sum(s.pass_count for s in model_signals if s.tier_name == tier)
        rate = passes / samples
        tier_signals.append(
            ModelTierRoiSignal(
                tier_name=tier,
                sample_count=samples,
                success_count=passes,
                success_rate=rate,
                suppressed=samples >= resolved_min and rate < resolved_floor,
            )
        )

    of_task = [r for r in rows if _as_text(r.get("task_type")) == task_type]
    undecided = sum(
        1
        for r in of_task
        if _as_text(r.get("verdict_status")) not in DOD_DECIDED_STATUSES
    )
    return ModelRoutingDodOverlay(
        task_type=task_type,
        tenant_id=tenant_id,
        joined_verdict_count=len(of_task),
        undecided_verdict_count=undecided,
        roi_overlay=ModelRoutingRoiOverlay(
            task_type=task_type,
            min_samples=resolved_min,
            success_floor=resolved_floor,
            signals=tuple(tier_signals),
        ),
        model_signals=model_signals,
    )


class ProtocolDodOutcomeReader(Protocol):
    """Reads the delegation x DoD verdict join for one task type and tenant."""

    def read_dod_outcomes(
        self, *, task_type: str, tenant_id: str
    ) -> list[dict[str, object]]: ...


def resolve_dod_read_tenant(tenant_value: object) -> str:
    """The canonical UUID string the join reads under -- resolved before any SQL.

    ``delegation_events.tenant_id`` is a UUID column (OMN-15683), so a legacy
    slug (the house tenant's ``omninode``) is mapped through the closed
    ``resolve_tenant_uuid`` table.

    Raises ``TenantContextMissingError`` when no tenant resolves under
    enforcement (OMN-16092: never a blinded read), and
    ``UnmappedTenantIdentityError`` for a value that is neither a UUID nor a
    mapped slug (never a guessed tenant).
    """
    tenant = resolve_rls_read_tenant(tenant_value, table=_TENANT_TABLE)
    try:
        return str(UUID(tenant))
    except ValueError:
        return str(resolve_tenant_uuid(tenant))


def join_delegations_to_verdicts(
    delegations: list[dict[str, object]],
    verdicts: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Pure join: each delegation run with every verdict that names it.

    ``delegations`` rows carry ``task_type``, ``correlation_id``, ``tier_name``,
    ``model_name`` and ``created_at``; ``verdicts`` rows carry
    ``verdict_correlation_id``, ``delegation_correlation_id``, ``verdict_status``,
    ``verdict_outcome`` and ``verdict_completed_at``. The key is compared as
    lower-cased text, because ``delegation_events.correlation_id`` is TEXT on the
    lab while ``dod_verify_runs.delegation_correlation_id`` is a UUID. The output
    is the row shape ``build_dod_overlay`` folds, in a stable order.
    """
    by_run: dict[str, list[dict[str, object]]] = {}
    for verdict in verdicts:
        key = str(verdict.get("delegation_correlation_id") or "").strip().lower()
        if key:
            by_run.setdefault(key, []).append(verdict)
    joined: list[dict[str, object]] = []
    for delegation in delegations:
        key = str(delegation.get("correlation_id") or "").strip().lower()
        for verdict in by_run.get(key, ()):
            row = dict(delegation)
            row["correlation_id"] = key
            for field in (
                "verdict_correlation_id",
                "verdict_status",
                "verdict_outcome",
                "verdict_completed_at",
            ):
                row[field] = verdict.get(field)
            joined.append(row)
    joined.sort(
        key=lambda r: (str(r["correlation_id"]), str(r.get("verdict_correlation_id")))
    )
    return joined


class PostgresDodOutcomeReader:
    """Read-only reader of the delegation x DoD verdict join (psycopg2).

    Two reads, each under the least privilege that can make it, joined in
    Python by ``join_delegations_to_verdicts``. No single lane principal reads
    both tables: the projection DSN's role reads ``public.delegation_events``
    and not the internal schema, and the internal DSN's role reads
    ``omninode_internal.dod_verify_runs`` and not ``delegation_events``. Widening
    either grant to make a SQL join possible would be the wrong trade.

    1. Verdicts that name a delegation run (internal DSN; the relation has no
       tenant column, it is platform-internal).
    2. The delegation rows for exactly those runs, for one task type and one
       tenant (projection DSN), in one transaction that sets ``app.tenant_id``
       and filters ``tenant_id`` explicitly.

    Connections are lazy, read-only, autocommit outside the tenant transaction,
    and bounded by a connect timeout.
    """

    def __init__(
        self,
        delegation_dsn: str,
        verdict_dsn: str,
        *,
        connect_timeout: int = _CONNECT_TIMEOUT_SECONDS,
        delegation_relation: str = DELEGATION_EVENTS_RELATION,
        verdict_relation: str = DOD_VERIFY_RUNS_RELATION,
    ) -> None:
        if not delegation_dsn or not verdict_dsn:
            raise ValueError("PostgresDodOutcomeReader requires two non-empty DSNs")
        for relation in (delegation_relation, verdict_relation):
            if not _RELATION_PATTERN.match(relation):
                raise ValueError(f"unsafe relation identifier: {relation!r}")
        self._delegation_dsn = delegation_dsn
        self._verdict_dsn = verdict_dsn
        self._connect_timeout = connect_timeout
        self._verdict_sql = (
            "SELECT correlation_id::text AS verdict_correlation_id, "
            "delegation_correlation_id::text AS delegation_correlation_id, "
            "status AS verdict_status, outcome AS verdict_outcome, "
            "completed_at AS verdict_completed_at "
            f"FROM {verdict_relation} "
            "WHERE delegation_correlation_id IS NOT NULL"
        )
        self._delegation_sql = (
            "SELECT task_type, correlation_id::text AS correlation_id, "
            "cost_tier_name AS tier_name, model_name, created_at "
            f"FROM {delegation_relation} "
            "WHERE task_type = %(task_type)s "
            "AND tenant_id = %(tenant_id)s::uuid "
            "AND lower(correlation_id::text) = ANY(%(run_ids)s)"
        )
        self._conns: dict[str, psycopg2.extensions.connection] = {}

    def _get_conn(self, dsn: str) -> psycopg2.extensions.connection:
        conn = self._conns.get(dsn)
        if conn is None or conn.closed:
            from omnimarket.projection.postgres_read_database import (
                connect_read_only,
            )

            conn = connect_read_only(dsn, connect_timeout=self._connect_timeout)
            self._conns[dsn] = conn
        return conn

    def read_dod_outcomes(
        self, *, task_type: str, tenant_id: str
    ) -> list[dict[str, object]]:
        import psycopg2.extras  # type: ignore[import-untyped]

        verdict_conn = self._get_conn(self._verdict_dsn)
        with verdict_conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(self._verdict_sql)
            verdicts = [dict(row) for row in cur.fetchall()]
        run_ids = sorted(
            {
                str(v["delegation_correlation_id"]).strip().lower()
                for v in verdicts
                if v.get("delegation_correlation_id")
            }
        )
        if not run_ids:
            return []

        conn = self._get_conn(self._delegation_dsn)
        conn.autocommit = False
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT set_config(%s, %s, true)", (TENANT_GUC, tenant_id))
                cur.execute(
                    self._delegation_sql,
                    {
                        "task_type": task_type,
                        "tenant_id": tenant_id,
                        "run_ids": run_ids,
                    },
                )
                delegations = [dict(row) for row in cur.fetchall()]
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.autocommit = True
        return join_delegations_to_verdicts(delegations, verdicts)

    def close(self) -> None:
        for conn in self._conns.values():
            if not conn.closed:
                conn.close()
        self._conns.clear()


def resolve_dod_outcome_reader() -> PostgresDodOutcomeReader | None:
    """The live reader, gated on both lane DSNs -- fail-open.

    ``OMNIDASH_ANALYTICS_DB_URL`` reads ``delegation_events``;
    ``OMNINODE_INTERNAL_DB_URL`` reads ``dod_verify_runs``. ``None`` when either
    is unset (no DoD read, static routing) or when the reader cannot be
    constructed. Construction does no I/O; the first read does.
    """
    delegation_dsn = os.environ.get(_ENV_DSN, "").strip()
    verdict_dsn = os.environ.get(_ENV_VERDICT_DSN, "").strip()
    if not delegation_dsn or not verdict_dsn:
        return None
    try:
        return PostgresDodOutcomeReader(delegation_dsn, verdict_dsn)
    except Exception:
        _logger.warning(
            "resolve_dod_outcome_reader failed to construct a reader from %s and "
            "%s; DoD routing read disabled (static routing)",
            _ENV_DSN,
            _ENV_VERDICT_DSN,
            exc_info=True,
        )
        return None


def resolve_dod_overlay(
    reader: ProtocolDodOutcomeReader,
    *,
    task_type: str,
    tenant_id: object = None,
    min_samples: int | None = None,
    success_floor: float | None = None,
    lookback_rows: int | None = None,
    window_seconds: float | None = None,
) -> ModelRoutingDodOverlay | None:
    """Read the join and fold it -- FAIL-OPEN on outage, FAIL-CLOSED on tenant.

    The tenant is resolved first, so a refusal issues no SQL. A missing tenant
    under enforcement propagates ``TenantContextMissingError``. An unmapped
    tenant value, or any failure of the read itself, returns ``None`` (static
    tier order).
    """
    try:
        tenant = resolve_dod_read_tenant(tenant_id)
    except UnmappedTenantIdentityError:
        # A tenant this repo has no UUID for cannot be read under honestly, and
        # it is a data defect in the request, not in routing: skip the read
        # (static routing) rather than refuse the delegation over telemetry.
        _logger.warning(
            "DoD routing read skipped for task_type=%s: tenant %r has no "
            "canonical UUID; static tiers",
            task_type,
            tenant_id,
        )
        return None
    try:
        rows = reader.read_dod_outcomes(task_type=task_type, tenant_id=tenant)
    except TenantContextMissingError:
        raise
    except Exception:
        _logger.warning(
            "DoD routing read failed for task_type=%s; falling back to static tiers",
            task_type,
            exc_info=True,
        )
        return None
    return build_dod_overlay(
        rows,
        task_type=task_type,
        tenant_id=tenant,
        min_samples=min_samples,
        success_floor=success_floor,
        lookback_rows=lookback_rows,
        window_seconds=window_seconds,
    )


def dod_roi_overlay_reader(
    reader: ProtocolDodOutcomeReader,
) -> Callable[[str], ModelRoutingRoiOverlay | None]:
    """Adapt the DoD read to the local dispatch port's ``roi_overlay_reader`` seam.

    The local port reads the overlay once per delegation under the install's
    own tenant. The returned callable logs the same DoD routing line the
    deployed lane logs, so both paths cite the rate they routed on.
    """

    def _read(task_type: str) -> ModelRoutingRoiOverlay | None:
        overlay = resolve_dod_overlay(reader, task_type=task_type)
        if overlay is None:
            return None
        _logger.info(
            "DoD routing read (local port): task_type=%s tenant=%s %s",
            task_type,
            overlay.tenant_id,
            overlay.describe(),
        )
        return overlay.roi_overlay

    return _read


__all__ = [
    "DELEGATION_EVENTS_RELATION",
    "DOD_DECIDED_STATUSES",
    "DOD_DONE_OUTCOME",
    "DOD_VERIFY_RUNS_RELATION",
    "ModelDodModelSignal",
    "ModelRoutingDodOverlay",
    "PostgresDodOutcomeReader",
    "ProtocolDodOutcomeReader",
    "build_dod_overlay",
    "dod_roi_overlay_reader",
    "join_delegations_to_verdicts",
    "resolve_dod_outcome_reader",
    "resolve_dod_overlay",
    "resolve_dod_read_tenant",
]
