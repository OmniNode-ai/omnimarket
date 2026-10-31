# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Metrics read from default-branch history and the repo-status log: reds, merges, regressions."""

from __future__ import annotations

import re
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta

from omnimarket.models.process_health import (
    EnumMetricKind,
    EnumPolarity,
    EnumSourceName,
    ModelMergeRecord,
    ModelMetric,
)
from omnimarket.nodes.node_process_health_compute.handlers.context import Context
from omnimarket.nodes.node_process_health_compute.handlers.metric_builder import (
    Computed,
    MetricSpec,
    breakdown_of,
    build_metric,
)
from omnimarket.nodes.node_process_health_compute.handlers.window import (
    Window,
    overlap_hours,
)

KIND_BOT = "bot"
KIND_LEDGER = "ledger"
KIND_DOCS = "docs"
KIND_PRODUCT = "product"


@dataclass(frozen=True)
class Episode:
    """A run of red verdicts for one repo."""

    repo: str
    start: datetime
    end: datetime
    checks: frozenset[str]


def merge_kind(ctx: Context, merge: ModelMergeRecord) -> str:
    """Bot, ledger roll, docs or product, in that order."""
    subject = merge.subject.lower()
    author = merge.author.lower()
    if any(
        marker.lower() in author or marker.lower() in subject
        for marker in ctx.config.bot_markers
    ):
        return KIND_BOT
    if merge.ledger_only or re.search(
        ctx.config.ledger_roll_subject_pattern, merge.subject, re.IGNORECASE
    ):
        return KIND_LEDGER
    if merge.docs_only:
        return KIND_DOCS
    return KIND_PRODUCT


def episodes(ctx: Context, ceiling: datetime) -> list[Episode]:
    """Red episodes per repo: from the first red verdict to the next verdict that is not red."""
    by_repo: dict[str, list[tuple[datetime, str, str | None]]] = defaultdict(list)
    for event in sorted(ctx.request.repo_status_events, key=lambda e: (e.repo, e.at)):
        by_repo[event.repo].append((event.at, event.verdict, event.check))
    found: list[Episode] = []
    for repo in sorted(by_repo):
        start: datetime | None = None
        checks: set[str] = set()
        for at, verdict, check in by_repo[repo]:
            if verdict == ctx.config.red_verdict:
                if start is None:
                    start = at
                    checks = set()
                if check:
                    checks.add(check)
            elif start is not None:
                found.append(Episode(repo, start, at, frozenset(checks)))
                start = None
        if start is not None:
            found.append(Episode(repo, start, max(start, ceiling), frozenset(checks)))
    return found


def _gap_hours(ctx: Context, start: datetime, end: datetime) -> float:
    read = ctx.reads.get(EnumSourceName.REPO_STATUS_EVENTS)
    if read is None:
        return 0.0
    return sum(overlap_hours(start, end, gap.start, gap.end) for gap in read.gaps)


def _reds_hours(ctx: Context, window: Window) -> Computed:
    hours: Counter[str] = Counter()
    for episode in episodes(ctx, ctx.request.generated_at):
        start, end = max(episode.start, window.start), min(episode.end, window.end)
        if end > start:
            hours[episode.repo] += (end - start).total_seconds() / 3600.0 - _gap_hours(
                ctx, start, end
            )
    clean = {repo: max(0.0, value) for repo, value in hours.items()}
    return Computed(
        sum(clean.values()),
        breakdown_of(clean),
        "repo-hours red, summed over repos; time inside a recorded log gap is not counted",
    )


def _reds_opened(ctx: Context, window: Window) -> Computed:
    opened: Counter[str] = Counter()
    for episode in episodes(ctx, ctx.request.generated_at):
        if window.contains(episode.start):
            opened[episode.repo] += 1
    return Computed(
        float(sum(opened.values())),
        breakdown_of({k: float(v) for k, v in opened.items()}),
    )


def _merges_in(ctx: Context, window: Window) -> list[ModelMergeRecord]:
    return [m for m in ctx.request.merges if window.contains(m.committed_at)]


def _merges_total(ctx: Context, window: Window) -> Computed:
    kinds = Counter(merge_kind(ctx, m) for m in _merges_in(ctx, window))
    return Computed(
        float(sum(kinds.values())),
        breakdown_of({k: float(v) for k, v in kinds.items()}),
    )


def _merges_product(ctx: Context, window: Window) -> Computed:
    repos = Counter(
        m.repo for m in _merges_in(ctx, window) if merge_kind(ctx, m) == KIND_PRODUCT
    )
    return Computed(
        float(sum(repos.values())),
        breakdown_of({k: float(v) for k, v in repos.items()}),
    )


def _ticket(ctx: Context, subject: str) -> str | None:
    match = re.search(ctx.config.ticket_pattern, subject)
    return match.group(0) if match else None


def _umbrella_tickets(ctx: Context) -> frozenset[str]:
    """Tickets that many PRs name are umbrellas (a landing worker's ticket), not one change's ticket."""
    prs: dict[str, set[tuple[str, int | None]]] = defaultdict(set)
    for merge in ctx.request.merges:
        ticket = _ticket(ctx, merge.subject)
        if ticket is not None:
            prs[ticket].add((merge.repo, merge.number))
    return frozenset(
        t for t, named in prs.items() if len(named) >= ctx.config.umbrella_min_prs
    )


def _fixed_forward(ctx: Context, window: Window) -> Computed:
    """Fix merges in the window whose ticket an earlier merge of another PR in the same repo already carried."""
    umbrellas = _umbrella_tickets(ctx)
    lookback = timedelta(hours=ctx.config.fix_forward_window_hours)
    product = [m for m in ctx.request.merges if merge_kind(ctx, m) == KIND_PRODUCT]
    repos: Counter[str] = Counter()
    for merge in _merges_in(ctx, window):
        if merge_kind(ctx, merge) != KIND_PRODUCT or not re.search(
            ctx.config.fix_subject_pattern, merge.subject
        ):
            continue
        ticket = _ticket(ctx, merge.subject)
        if ticket is None or ticket in umbrellas:
            continue
        for earlier in product:
            if (
                earlier.repo == merge.repo
                and earlier.number != merge.number
                and earlier.committed_at < merge.committed_at
                and merge.committed_at - earlier.committed_at <= lookback
                and _ticket(ctx, earlier.subject) == ticket
            ):
                repos[merge.repo] += 1
                break
    return Computed(
        float(sum(repos.values())),
        breakdown_of({k: float(v) for k, v in repos.items()}),
        "a fix merge naming the ticket of an earlier merge of another PR in the same repo inside the look-back, umbrella tickets (named by many PRs) excluded; a heuristic on subjects",
    )


def _reverted(ctx: Context, window: Window) -> Computed:
    repos = Counter(
        m.repo
        for m in _merges_in(ctx, window)
        if re.search(ctx.config.revert_subject_pattern, m.subject)
    )
    return Computed(
        float(sum(repos.values())),
        breakdown_of({k: float(v) for k, v in repos.items()}),
    )


def _controller_to_merged(ctx: Context, window: Window) -> Computed:
    first: dict[str, datetime] = {}
    for decision in sorted(ctx.request.decisions, key=lambda d: d.ts):
        first.setdefault(decision.pr, decision.ts)
    minutes: list[float] = []
    for merge in _merges_in(ctx, window):
        if merge_kind(ctx, merge) != KIND_PRODUCT or merge.number is None:
            continue
        seen = first.get(f"{merge.repo}#{merge.number}")
        if seen is not None and seen <= merge.committed_at:
            minutes.append((merge.committed_at - seen).total_seconds() / 60.0)
    if not minutes:
        return Computed(
            None,
            note="no product merge in the window has a controller record before its commit",
        )
    return Computed(
        statistics.median(minutes),
        note=f"median of {len(minutes)} merges, from the controller's first decision row to the squash commit; understates the wait",
    )


def _green_to_merged(ctx: Context, window: Window) -> Computed:
    pairs = [
        (m.median_green_to_merged_min, m.green_to_merged_n)
        for m in ctx.request.landing_metrics
        if window.contains(m.window_end)
        and m.median_green_to_merged_min is not None
        and m.green_to_merged_n > 0
    ]
    total = sum(n for _, n in pairs)
    if total == 0:
        return Computed(
            None,
            note="no landing-metrics record in the window measured a green-to-merged time",
        )
    mean = sum(value * n for value, n in pairs if value is not None) / total
    return Computed(
        mean,
        note=f"mean of {len(pairs)} window medians weighted by their {total} merges; a median of medians cannot be exact",
    )


def repo_metrics(ctx: Context, window: Window) -> list[ModelMetric]:
    """Metrics whose sources are the repo-status log, default-branch history and the controller files."""
    reads = ctx.reads
    spec = MetricSpec
    return [
        build_metric(
            spec(
                "reds.hours_red",
                "Repo-hours a dev branch stood red",
                "repo-hours",
                EnumMetricKind.HOURS,
                EnumPolarity.LOWER_IS_BETTER,
                (EnumSourceName.REPO_STATUS_EVENTS,),
                "Repo-status verdict events: a red episode runs from the first red verdict to the next verdict that is not red; clipped to the window.",
            ),
            reads,
            window,
            lambda: _reds_hours(ctx, window),
        ),
        build_metric(
            spec(
                "reds.episodes_opened",
                "Dev-branch red episodes opened",
                "episodes",
                EnumMetricKind.COUNT,
                EnumPolarity.LOWER_IS_BETTER,
                (EnumSourceName.REPO_STATUS_EVENTS,),
                "Repo-status verdict events: episodes whose first red verdict falls in the window.",
            ),
            reads,
            window,
            lambda: _reds_opened(ctx, window),
        ),
        build_metric(
            spec(
                "regressions.fixed_forward",
                "Merges later fixed forward",
                "fix merges",
                EnumMetricKind.COUNT,
                EnumPolarity.LOWER_IS_BETTER,
                (EnumSourceName.GIT_MERGES,),
                "Default-branch history: a fix merge naming the ticket of an earlier merge of another PR in the same repo.",
            ),
            reads,
            window,
            lambda: _fixed_forward(ctx, window),
        ),
        build_metric(
            spec(
                "regressions.reverted",
                "Merges reverted",
                "reverts",
                EnumMetricKind.COUNT,
                EnumPolarity.LOWER_IS_BETTER,
                (EnumSourceName.GIT_MERGES,),
                "Default-branch history: merges whose subject opens with a revert marker the contract declares.",
            ),
            reads,
            window,
            lambda: _reverted(ctx, window),
        ),
        build_metric(
            spec(
                "merges.total",
                "Merges",
                "merges",
                EnumMetricKind.COUNT,
                EnumPolarity.NEUTRAL,
                (EnumSourceName.GIT_MERGES,),
                "First-parent squash commits on each default branch; the breakdown is bot, ledger roll, docs and product.",
            ),
            reads,
            window,
            lambda: _merges_total(ctx, window),
        ),
        build_metric(
            spec(
                "merges.product",
                "Product merges",
                "merges",
                EnumMetricKind.COUNT,
                EnumPolarity.HIGHER_IS_BETTER,
                (EnumSourceName.GIT_MERGES,),
                "Merges that are not bot, ledger roll or docs; the breakdown is the repo.",
            ),
            reads,
            window,
            lambda: _merges_product(ctx, window),
        ),
        build_metric(
            spec(
                "merges.controller_to_merged_median_min",
                "Controller first decision to merge",
                "minutes",
                EnumMetricKind.MEDIAN_MINUTES,
                EnumPolarity.LOWER_IS_BETTER,
                (EnumSourceName.GIT_MERGES, EnumSourceName.LANDING_DECISIONS),
                "Median, over product merges, from the landing controller's first decision row for the PR to its squash commit.",
            ),
            reads,
            window,
            lambda: _controller_to_merged(ctx, window),
        ),
        build_metric(
            spec(
                "merges.green_to_merged_min",
                "Green to merged",
                "minutes",
                EnumMetricKind.MEDIAN_MINUTES,
                EnumPolarity.LOWER_IS_BETTER,
                (EnumSourceName.LANDING_METRICS_HISTORY,),
                "Landing metrics history: the median green-to-merged minutes of each measured window ending in this window, weighted by its merges.",
            ),
            reads,
            window,
            lambda: _green_to_merged(ctx, window),
        ),
    ]
