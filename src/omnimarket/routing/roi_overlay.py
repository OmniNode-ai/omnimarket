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
    is fail-OPEN: any read error / missing table / unreachable DB returns ``None``,
    so a telemetry outage degrades to the static tier order and NEVER breaks
    routing.

The ``context_roi_scores`` table carries no ``task_type`` column; its rows are the
context-ROI experiment's per-(task x arm x model) generation outcomes. Each row's
``endpoint_ref`` (a routing-tier backend id, e.g. ``local-coder``) maps to a tier
via the routing authority (``tier_for_backend``, injected here as
``tier_of_endpoint`` to keep this module free of a reducer import). We therefore
aggregate the observed generation success rate PER TIER and apply it to the
requested ``task_type``. This is the honest first-slice signal: "this tier
empirically fails the generation work we measure, so stop starting there." The
per-task_type refinement is tracked as follow-up.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

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


def build_roi_overlay(
    rows: list[dict[str, object]],
    *,
    task_type: str,
    tier_of_endpoint: Callable[[str], str | None],
    min_samples: int | None = None,
    success_floor: float | None = None,
) -> ModelRoutingRoiOverlay:
    """Aggregate ``context_roi_scores`` rows into a deterministic ROI overlay.

    Rows are grouped by the routing tier their ``endpoint_ref`` maps to (via the
    injected ``tier_of_endpoint`` — the routing authority's ``tier_for_backend``).
    Rows whose endpoint does not map to a known tier are ignored (they cannot
    inform a tier decision). A tier is suppressed when it has ``>= min_samples``
    rows AND its ``final_success`` rate is strictly below ``success_floor``.

    Pure and deterministic: signals are emitted in sorted tier-name order so the
    same rows always yield the same overlay.
    """
    resolved_min = min_samples if min_samples is not None else resolve_roi_min_samples()
    resolved_floor = (
        success_floor if success_floor is not None else resolve_roi_success_floor()
    )

    counts: dict[str, list[int]] = {}  # tier -> [success_count, sample_count]
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
) -> ModelRoutingRoiOverlay | None:
    """Read ``context_roi_scores`` and build the ROI overlay — FAIL-OPEN.

    Reads every captured row through the ``DatabaseAdapter.query`` protocol
    (in-memory / SQLite / asyncpg all satisfy it) and aggregates via
    ``build_roi_overlay``. Returns ``None`` on ANY failure — missing table,
    unreachable DB, adapter error — so a telemetry outage degrades to the static
    tier order and never breaks a live routing decision. An empty (but readable)
    table returns an overlay with no signals, which the reducer treats exactly
    like the static path.
    """
    try:
        rows = db.query(CONTEXT_ROI_TABLE)
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
    """Resolve a read-only projection adapter for ``context_roi_scores`` — FAIL-OPEN.

    Gated on the ``OMNIDASH_ANALYTICS_DB_URL`` DSN (the projection DB that
    materialises the table). Returns ``None`` when the DSN is unset (the common
    local case — no ROI read, static routing) or on ANY construction error, so a
    delegation never fails because a telemetry DB is absent or unreachable. The
    adapter connects LAZILY with a bounded timeout, so this factory does no I/O —
    the first actual read happens inside ``resolve_roi_overlay``, which is itself
    wrapped fail-open.

    This is the live-wiring entrypoint (OMN-14001): the delegation dispatch-port
    selector calls it to point the ROI reader at the real projection DB. It is a
    deliberate, documented exception to the fail-fast-on-missing-env rule — a
    telemetry read must degrade to static routing, never crash the caller.
    """
    dsn = os.environ.get(_ENV_CONTEXT_ROI_DSN, "").strip()
    if not dsn:
        return None
    try:
        from omnimarket.projection.postgres_read_database import (
            PostgresReadDatabaseAdapter,
        )

        return PostgresReadDatabaseAdapter(dsn)
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
    "resolve_roi_min_samples",
    "resolve_roi_overlay",
    "resolve_roi_success_floor",
]
