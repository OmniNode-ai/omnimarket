# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Anonymised sufficient statistics, sharing the metric calculators' classification rules.

No raw log text, ticket, repository or worker identifiers enter the overlay. Identities
are opaque hashes used solely to deduplicate observations inside a selected window.
"""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Literal

from omnimarket.models.process_health import (
    ModelProcessMeasureSeries,
    ModelProcessObservation,
    ModelProcessRangeOverlay,
)
from omnimarket.nodes.node_process_health_compute.handlers.calc_landing import _is_bug
from omnimarket.nodes.node_process_health_compute.handlers.calc_ledger import (
    _andon_intervals,
    _classes,
    _friction_bucket,
    _is_andon,
    _is_landing_lane,
    _outcome_class,
)
from omnimarket.nodes.node_process_health_compute.handlers.calc_repo import (
    KIND_PRODUCT,
    episodes,
    fixed_forward_merges,
    merge_kind,
)
from omnimarket.nodes.node_process_health_compute.handlers.context import Context
from omnimarket.nodes.node_process_health_compute.handlers.ledger_view import (
    cost_minutes,
    leading_int,
)
from omnimarket.nodes.node_process_health_compute.handlers.window import Window


def _identity(*parts: str) -> str:
    return hashlib.sha256("\0".join(parts).encode()).hexdigest()[:24]


def build_range_overlay(ctx: Context) -> ModelProcessRangeOverlay:
    """Publish point contributions and clipped-duration intervals, never bucket averages."""
    req, cfg = ctx.request, ctx.config
    start = min(
        req.generated_at - timedelta(days=cfg.range_history_days),
        cfg.reset_at - (req.generated_at - cfg.reset_at),
    )
    whole = Window(start, req.generated_at)
    points: defaultdict[str, list[ModelProcessObservation]] = defaultdict(list)
    reducers: dict[
        str, Literal["sum", "ratio", "median", "weighted_mean", "hours", "union_hours"]
    ] = {}
    unavailable: dict[str, str] = {}

    def add(
        key: str,
        at: datetime,
        value: float = 1.0,
        *,
        end: datetime | None = None,
        identity: str = "",
        weight: float = 1.0,
        unmeasured_reason: str = "",
    ) -> None:
        if end is None and not whole.contains(at):
            return
        if end is not None and (end <= start or at >= req.generated_at):
            return
        points[key].append(
            ModelProcessObservation(
                at=at,
                end=end,
                value=value,
                identity=identity,
                weight=weight,
                unmeasured_reason=unmeasured_reason,
            )
        )

    for row in ctx.ledger:
        if row.rtype == "FRICTION":
            add("friction.rows", row.ts)
            if row.cells.get("class") == cfg.refusal_class:
                add("validation.refusals", row.ts)
            if any(
                cfg.preflight_guard_marker.lower() in row.cells.get(k, "").lower()
                for k in ("guard", "reason")
            ):
                add("validation.preflight_blocks", row.ts)
            bucket = _friction_bucket(ctx, row)
            minutes = cost_minutes(row.cells.get("cost", ""))
            if minutes is not None:
                add(f"friction.{bucket}_minutes", row.ts, minutes)
            else:
                add(
                    f"friction.{bucket}_minutes",
                    row.ts,
                    0,
                    unmeasured_reason="A log entry in this range has no parseable cost.",
                )
        if row.rtype == "HOLD" and _is_andon(ctx, row):
            add("andon.stops_started", row.ts)
        if row.rtype != "TERMINAL":
            continue
        # Match _terminals: last row with an outcome wins, including across buckets.
        claim = row.cells.get(
            "closes-CLAIM", row.cells.get("closes-claim", row.ts.isoformat())
        )
        identity = _identity(row.lane, claim)
        # A row with no outcome must not displace one that has an outcome.
        # Filter only against rows in a query at the consumer, using the weight as priority.
        priority = 2.0 if "outcome" in row.cells else 1.0
        add("lanes.terminals", row.ts, identity=identity, weight=priority)
        klass = _outcome_class(ctx, row)
        for category in _classes(ctx):
            key = f"lanes.share_{category.replace('-', '_')}"
            reducers[key] = "ratio"
            add(
                key,
                row.ts,
                float(klass == category),
                identity=identity,
                weight=priority,
            )
        if not _is_landing_lane(ctx, row):
            key = "delegation.lane_share"
            reducers[key] = "ratio"
            add(
                key,
                row.ts,
                float((leading_int(row.cells.get("delegated", "")) or 0) >= 1),
                identity=identity,
                weight=priority,
            )

    reducers["andon.hours_stopped"] = "union_hours"
    for begin, end in _andon_intervals(ctx, req.generated_at):
        add("andon.hours_stopped", begin, end=end)
    reducers["reds.hours_red"] = "hours"
    for episode in episodes(ctx, req.generated_at):
        add("reds.episodes_opened", episode.start)
        add("reds.hours_red", episode.start, end=episode.end)

    for merge in req.merges:
        add("merges.total", merge.committed_at)
        if merge_kind(ctx, merge) == KIND_PRODUCT:
            add("merges.product", merge.committed_at)
        if re.search(cfg.revert_subject_pattern, merge.subject):
            add("regressions.reverted", merge.committed_at)
    for merge in fixed_forward_merges(ctx, whole):
        add("regressions.fixed_forward", merge.committed_at)

    first: dict[str, datetime] = {}
    for decision in sorted(req.decisions, key=lambda d: d.ts):
        first.setdefault(decision.pr, decision.ts)
        if (
            decision.kind == cfg.fix_dispatch_kind
            and decision.outcome == cfg.fix_dispatch_outcome
        ):
            add(
                "validation.required_check_failures",
                decision.ts,
                identity=_identity(decision.pr, decision.head),
            )
    reducers["merges.controller_to_merged_median_min"] = "median"
    for merge in req.merges:
        seen = first.get(f"{merge.repo}#{merge.number}")
        if (
            merge_kind(ctx, merge) == KIND_PRODUCT
            and merge.number is not None
            and seen is not None
            and seen <= merge.committed_at
        ):
            add(
                "merges.controller_to_merged_median_min",
                merge.committed_at,
                (merge.committed_at - seen).total_seconds() / 60,
            )
    reducers["merges.green_to_merged_min"] = "weighted_mean"
    for record in req.landing_metrics:
        if (
            record.median_green_to_merged_min is not None
            and record.green_to_merged_n > 0
        ):
            add(
                "merges.green_to_merged_min",
                record.window_end,
                record.median_green_to_merged_min,
                weight=record.green_to_merged_n,
            )
    released: defaultdict[str, list[datetime]] = defaultdict(list)
    gap = timedelta(minutes=cfg.relapse_min_gap_minutes)
    for lease in sorted(req.leases, key=lambda x: x.spawned_at):
        add("friction.measured_lease_minutes", lease.released_at, lease.worker_minutes)
        if lease.kind == "cause":
            if any(lease.spawned_at - done >= gap for done in released[lease.subject]):
                add("regressions.cause_relapses", lease.spawned_at)
            released[lease.subject].append(lease.released_at)
    reducers["delegation.landing_share"] = "ratio"
    for delegation in req.delegation_rows:
        add(
            "delegation.landing_share",
            delegation.ts,
            float(delegation.delegated >= 1),
            identity=_identity(str(delegation.lease_id)),
        )
    for issue in req.linear_issues:
        if _is_bug(ctx, issue):
            add("bugs.opened", issue.created_at)
            if issue.completed_at is not None:
                add("bugs.closed", issue.completed_at)
        if issue.history_read:
            for moment in issue.reopened_at:
                add("regressions.tickets_reopened", moment)
    if not req.linear_issues or not all(i.history_read for i in req.linear_issues):
        unavailable["regressions.tickets_reopened"] = (
            "The snapshot does not hold every issue's state history."
        )

    # The descriptor list is obtained from the existing calculators by the caller;
    # explicit empty series matter: measured no-events is a zero, absent data is not.
    from omnimarket.nodes.node_process_health_compute.handlers.calc_landing import (
        landing_metrics,
    )
    from omnimarket.nodes.node_process_health_compute.handlers.calc_ledger import (
        ledger_metrics,
    )
    from omnimarket.nodes.node_process_health_compute.handlers.calc_repo import (
        repo_metrics,
    )

    keys = {
        m.key
        for m in [
            *ledger_metrics(ctx, whole),
            *repo_metrics(ctx, whole),
            *landing_metrics(ctx, whole),
        ]
    }
    return ModelProcessRangeOverlay(
        start=start,
        end=req.generated_at,
        bucket_hours=cfg.graph_bucket_hours,
        flat_threshold_ratio=cfg.flat_threshold_ratio,
        measures=tuple(
            ModelProcessMeasureSeries(
                key=key,
                reducer=reducers.get(key, "sum"),
                observations=tuple(sorted(points[key], key=lambda p: p.at)),
                unavailable_reason=unavailable.get(key, ""),
            )
            for key in sorted(keys)
        ),
    )
