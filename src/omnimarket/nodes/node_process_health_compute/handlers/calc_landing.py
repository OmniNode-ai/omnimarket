# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Metrics read from the landing controller files and the Linear snapshot."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta

from omnimarket.models.process_health import (
    EnumMetricKind,
    EnumPolarity,
    EnumSourceName,
    ModelLinearIssue,
    ModelMetric,
)
from omnimarket.nodes.node_process_health_compute.handlers.context import Context
from omnimarket.nodes.node_process_health_compute.handlers.metric_builder import (
    Computed,
    MetricSpec,
    breakdown_of,
    build_metric,
)
from omnimarket.nodes.node_process_health_compute.handlers.window import Window

_KIND_CAUSE = "cause"


def _failing_heads(ctx: Context, window: Window) -> Computed:
    heads: set[tuple[str, str]] = set()
    for decision in ctx.request.decisions:
        if (
            window.contains(decision.ts)
            and decision.kind == ctx.config.fix_dispatch_kind
            and decision.outcome == ctx.config.fix_dispatch_outcome
        ):
            heads.add((decision.pr, decision.head))
    repos = Counter(pr.split("#", 1)[0] for pr, _ in heads)
    return Computed(
        float(len(heads)),
        breakdown_of({k: float(v) for k, v in repos.items()}),
        "distinct (PR, head) pairs the controller spawned a fix worker for: a required check red or a conflict on that head",
    )


def _relapses(ctx: Context, window: Window) -> Computed:
    gap = timedelta(minutes=ctx.config.relapse_min_gap_minutes)
    released: dict[str, list[datetime]] = defaultdict(list)
    causes = sorted(
        (lease for lease in ctx.request.leases if lease.kind == _KIND_CAUSE),
        key=lambda lease: lease.spawned_at,
    )
    repos: Counter[str] = Counter()
    for lease in causes:
        earlier = released[lease.subject]
        if window.contains(lease.spawned_at) and any(
            lease.spawned_at - done >= gap for done in earlier
        ):
            repos[lease.repo] += 1
        earlier.append(lease.released_at)
    return Computed(
        float(sum(repos.values())),
        breakdown_of({k: float(v) for k, v in repos.items()}),
        "a fix cause leased again after an earlier lease on it ended; the cause key is the defect class",
    )


def _lease_minutes(ctx: Context, window: Window) -> Computed:
    kinds: defaultdict[str, float] = defaultdict(float)
    for lease in ctx.request.leases:
        if window.contains(lease.released_at):
            kinds[lease.kind] += lease.worker_minutes
    return Computed(
        sum(kinds.values()),
        breakdown_of(dict(kinds)),
        "controller lease wall time from spawn to release",
    )


def _landing_delegation(ctx: Context, window: Window) -> Computed:
    latest = {}
    for row in sorted(ctx.request.delegation_rows, key=lambda r: r.ts):
        if window.contains(row.ts):
            latest[row.lease_id] = row
    if not latest:
        return Computed(None, note="no delegation row in the window")
    delegated = sum(1 for row in latest.values() if row.delegated >= 1)
    return Computed(
        delegated / len(latest), note=f"{delegated} of {len(latest)} landing leases"
    )


def _is_bug(ctx: Context, issue: ModelLinearIssue) -> bool:
    wanted = {label.lower() for label in ctx.config.bug_labels}
    return any(label.lower() in wanted for label in issue.labels)


def _bugs_opened(ctx: Context, window: Window) -> Computed:
    count = sum(
        1
        for i in ctx.request.linear_issues
        if _is_bug(ctx, i) and window.contains(i.created_at)
    )
    return Computed(float(count))


def _bugs_closed(ctx: Context, window: Window) -> Computed:
    count = sum(
        1
        for i in ctx.request.linear_issues
        if _is_bug(ctx, i)
        and i.completed_at is not None
        and window.contains(i.completed_at)
    )
    return Computed(float(count))


def _reopened(ctx: Context, window: Window) -> Computed:
    with_history = [i for i in ctx.request.linear_issues if i.history_read]
    total = len(ctx.request.linear_issues)
    if not with_history:
        return Computed(
            None,
            unread_reason=f"the snapshot holds no state history for any of its {total} issues, so a reopen cannot be seen",
        )
    count = sum(
        1 for i in with_history for moment in i.reopened_at if window.contains(moment)
    )
    partial = (
        f"state history is held for {len(with_history)} of {total} snapshot issues"
        if len(with_history) != total
        else ""
    )
    return Computed(float(count), partial_reason=partial)


def landing_metrics(ctx: Context, window: Window) -> list[ModelMetric]:
    """Metrics from the controller files and the Linear snapshot."""
    reads = ctx.reads
    spec = MetricSpec
    out = [
        build_metric(
            spec(
                "validation.required_check_failures",
                "Pull request heads with a real red",
                "heads",
                EnumMetricKind.COUNT,
                EnumPolarity.LOWER_IS_BETTER,
                (EnumSourceName.LANDING_DECISIONS,),
                "Landing controller decision rows of the fix-dispatch kind and outcome the contract names, counted once per PR head.",
            ),
            reads,
            window,
            lambda: _failing_heads(ctx, window),
        ),
        build_metric(
            spec(
                "regressions.cause_relapses",
                "Fix causes that came back",
                "leases",
                EnumMetricKind.COUNT,
                EnumPolarity.LOWER_IS_BETTER,
                (EnumSourceName.LANDING_COST,),
                "Cause leases from the controller cost file whose cause key had an earlier lease that ended at least the contract's gap before.",
            ),
            reads,
            window,
            lambda: _relapses(ctx, window),
        ),
        build_metric(
            spec(
                "friction.measured_lease_minutes",
                "Controller lease minutes (measured)",
                "minutes",
                EnumMetricKind.MINUTES,
                EnumPolarity.NEUTRAL,
                (EnumSourceName.LANDING_COST,),
                "Wall minutes from spawn to release of fix-worker leases released in the window: measured lane time, not an estimate.",
            ),
            reads,
            window,
            lambda: _lease_minutes(ctx, window),
        ),
        build_metric(
            spec(
                "delegation.landing_share",
                "Landing leases that delegated",
                "share of leases",
                EnumMetricKind.RATIO,
                EnumPolarity.HIGHER_IS_BETTER,
                (EnumSourceName.LANDING_DELEGATION,),
                "Controller delegation rows, one per lease, with delegated of 1 or more over all leases.",
            ),
            reads,
            window,
            lambda: _landing_delegation(ctx, window),
        ),
        build_metric(
            spec(
                "bugs.opened",
                "Bug tickets opened",
                "tickets",
                EnumMetricKind.COUNT,
                EnumPolarity.LOWER_IS_BETTER,
                (EnumSourceName.LINEAR_SNAPSHOT,),
                "Linear snapshot: issues carrying a bug label, by created time.",
            ),
            reads,
            window,
            lambda: _bugs_opened(ctx, window),
        ),
        build_metric(
            spec(
                "bugs.closed",
                "Bug tickets closed",
                "tickets",
                EnumMetricKind.COUNT,
                EnumPolarity.HIGHER_IS_BETTER,
                (EnumSourceName.LINEAR_SNAPSHOT,),
                "Linear snapshot: issues carrying a bug label, by completed time.",
            ),
            reads,
            window,
            lambda: _bugs_closed(ctx, window),
        ),
        build_metric(
            spec(
                "regressions.tickets_reopened",
                "Closed tickets reopened",
                "reopens",
                EnumMetricKind.COUNT,
                EnumPolarity.LOWER_IS_BETTER,
                (EnumSourceName.LINEAR_SNAPSHOT,),
                "Linear snapshot state history: a move from a completed or canceled state back to an open one.",
            ),
            reads,
            window,
            lambda: _reopened(ctx, window),
        ),
    ]
    return out
