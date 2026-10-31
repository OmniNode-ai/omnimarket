# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Metrics read from ledger rows: refusals, lane outcomes, andon stops, friction, delegation."""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import datetime
from functools import partial

from omnimarket.models.process_health import (
    EnumMetricKind,
    EnumPolarity,
    EnumSourceName,
    ModelMetric,
)
from omnimarket.nodes.node_process_health_compute.handlers.context import Context
from omnimarket.nodes.node_process_health_compute.handlers.ledger_view import (
    ParsedRow,
    cost_minutes,
    leading_int,
    parse_stamp,
)
from omnimarket.nodes.node_process_health_compute.handlers.metric_builder import (
    Computed,
    MetricSpec,
    breakdown_of,
    build_metric,
)
from omnimarket.nodes.node_process_health_compute.handlers.window import Window

_LEDGER = (EnumSourceName.LEDGER,)
_FRICTION_METHOD = "FRICTION rows by their stamp; the class= cell names the class."


def _rows(ctx: Context, window: Window, rtype: str) -> list[ParsedRow]:
    return [row for row in ctx.ledger if row.rtype == rtype and window.contains(row.ts)]


def _is_reporter(ctx: Context, row: ParsedRow) -> bool:
    actor = row.cells.get("actor", "")
    return any(
        actor.startswith(prefix) for prefix in ctx.config.reporter_actor_prefixes
    )


def _is_landing_lane(ctx: Context, row: ParsedRow) -> bool:
    return row.lane.startswith(ctx.config.landing_lane_prefix)


def _terminals(ctx: Context, window: Window) -> list[ParsedRow]:
    """One TERMINAL per lane and CLAIM: the runner and the lane both write one; the last with an outcome wins."""
    chosen: dict[tuple[str, str], ParsedRow] = {}
    for row in _rows(ctx, window, "TERMINAL"):
        key = (
            row.lane,
            row.cells.get(
                "closes-CLAIM", row.cells.get("closes-claim", row.ts.isoformat())
            ),
        )
        previous = chosen.get(key)
        if (
            previous is None
            or "outcome" in row.cells
            or "outcome" not in previous.cells
        ):
            chosen[key] = row
    return sorted(chosen.values(), key=lambda row: (row.ts, row.lane))


def _outcome_class(ctx: Context, row: ParsedRow) -> str:
    if _is_landing_lane(ctx, row):
        result = row.cells.get("result", "")
        if result in ctx.config.landing_failed_results:
            return ctx.config.landing_failed_class
    outcome = row.cells.get("outcome", "").lower()
    for klass, members in ctx.config.lane_outcome_classes.items():
        if outcome in {member.lower() for member in members}:
            return klass
    return ctx.config.other_outcome_class


def _classes(ctx: Context) -> list[str]:
    return sorted(
        {
            *ctx.config.lane_outcome_classes,
            ctx.config.other_outcome_class,
            ctx.config.landing_failed_class,
        }
    )


def _friction_class(ctx: Context, row: ParsedRow) -> str:
    return row.cells.get("class") or ctx.config.unclassified_label


def _refusals(ctx: Context, window: Window) -> Computed:
    counts: Counter[str] = Counter()
    for row in _rows(ctx, window, "FRICTION"):
        if row.cells.get("class") == ctx.config.refusal_class:
            counts[
                row.cells.get("reason")
                or row.cells.get("guard")
                or ctx.config.unclassified_label
            ] += 1
    return Computed(
        float(sum(counts.values())),
        breakdown_of({k: float(v) for k, v in counts.items()}),
    )


def _preflight(ctx: Context, window: Window) -> Computed:
    marker = ctx.config.preflight_guard_marker.lower()
    counts: Counter[str] = Counter()
    for row in _rows(ctx, window, "FRICTION"):
        guard = row.cells.get("guard", "")
        if marker in guard.lower() or marker in row.cells.get("reason", "").lower():
            counts[guard or row.cells.get("reason", "")] += 1
    return Computed(
        float(sum(counts.values())),
        breakdown_of({k: float(v) for k, v in counts.items()}),
    )


def _share(ctx: Context, window: Window, klass: str) -> Computed:
    terminals = _terminals(ctx, window)
    total = len(terminals)
    counts = Counter(_outcome_class(ctx, row) for row in terminals)
    if total == 0:
        return Computed(None, note="no lane ended in the window")
    return Computed(
        counts[klass] / total, note=f"{counts[klass]} of {total} lane TERMINALs"
    )


def _terminal_count(ctx: Context, window: Window) -> Computed:
    terminals = _terminals(ctx, window)
    counts = Counter(_outcome_class(ctx, row) for row in terminals)
    return Computed(
        float(len(terminals)), breakdown_of({k: float(v) for k, v in counts.items()})
    )


def _is_andon(ctx: Context, row: ParsedRow) -> bool:
    reason = row.cells.get("reason", "").lower()
    if any(
        reason.startswith(prefix.lower()) for prefix in ctx.config.andon_reason_prefixes
    ):
        return True
    free = row.text.split(" | ")[-1].strip().lower()
    return any(
        free.startswith(prefix.lower()) for prefix in ctx.config.andon_text_prefixes
    )


def _hold_id(row: ParsedRow) -> str:
    return row.cells.get("id", f"{row.ts.strftime('%Y-%m-%dT%H:%M:%SZ')}-{row.lane}")


def _andon_intervals(
    ctx: Context, ceiling: datetime
) -> list[tuple[datetime, datetime]]:
    releases: dict[str, datetime] = {}
    for row in ctx.ledger:
        if row.rtype == "RELEASE" and "re" in row.cells:
            releases.setdefault(row.cells["re"], row.ts)
    intervals: list[tuple[datetime, datetime]] = []
    for row in ctx.ledger:
        if row.rtype != "HOLD" or not _is_andon(ctx, row):
            continue
        end = ceiling
        until = row.cells.get("until")
        if until and re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?Z", until):
            end = min(end, parse_stamp(until))
        released = releases.get(_hold_id(row))
        if released is not None:
            end = min(end, released)
        if end > row.ts:
            intervals.append((row.ts, end))
    return intervals


def _union_hours(intervals: list[tuple[datetime, datetime]], window: Window) -> float:
    clipped = sorted(
        (max(start, window.start), min(end, window.end))
        for start, end in intervals
        if min(end, window.end) > max(start, window.start)
    )
    total = 0.0
    current_start: datetime | None = None
    current_end: datetime | None = None
    for start, end in clipped:
        if current_end is None or start > current_end:
            if current_start is not None and current_end is not None:
                total += (current_end - current_start).total_seconds()
            current_start, current_end = start, end
        else:
            current_end = max(current_end, end)
    if current_start is not None and current_end is not None:
        total += (current_end - current_start).total_seconds()
    return total / 3600.0


def _andon_hours(ctx: Context, window: Window) -> Computed:
    intervals = _andon_intervals(ctx, ctx.request.generated_at)
    hours = _union_hours(intervals, window)
    return Computed(
        hours,
        note="a HOLD with no RELEASE and no until counts as in force until the generated-at time",
    )


def _andon_stops(ctx: Context, window: Window) -> Computed:
    holds = [row for row in _rows(ctx, window, "HOLD") if _is_andon(ctx, row)]
    counts = Counter(
        row.cells.get("reason", "andon").split(":", 1)[0] or "andon" for row in holds
    )
    return Computed(
        float(len(holds)), breakdown_of({k: float(v) for k, v in counts.items()})
    )


def _friction_rows(ctx: Context, window: Window) -> Computed:
    counts = Counter(
        _friction_class(ctx, row) for row in _rows(ctx, window, "FRICTION")
    )
    return Computed(
        float(sum(counts.values())),
        breakdown_of({k: float(v) for k, v in counts.items()}),
    )


def _friction_bucket(ctx: Context, row: ParsedRow) -> str:
    cost = row.cells.get("cost", "").strip().lower()
    if _friction_class(ctx, row) in ctx.config.capacity_estimate_classes or any(
        cost.startswith(prefix.lower()) for prefix in ctx.config.estimate_cost_prefixes
    ):
        return "capacity_estimate"
    return "reporter_stated" if _is_reporter(ctx, row) else "lane_stated"


def _friction_minutes(ctx: Context, window: Window, bucket: str) -> Computed:
    minutes: defaultdict[str, float] = defaultdict(float)
    unparsed = 0
    for row in _rows(ctx, window, "FRICTION"):
        if _friction_bucket(ctx, row) != bucket:
            continue
        value = cost_minutes(row.cells.get("cost", ""))
        if value is None:
            unparsed += 1
            continue
        minutes[_friction_class(ctx, row)] += value
    note = f"{unparsed} rows in this bucket carry no parseable cost" if unparsed else ""
    return Computed(sum(minutes.values()), breakdown_of(dict(minutes)), note)


def _delegation_share(ctx: Context, window: Window) -> Computed:
    lanes = [row for row in _terminals(ctx, window) if not _is_landing_lane(ctx, row)]
    if not lanes:
        return Computed(None, note="no non-landing lane ended in the window")
    delegated = sum(
        1 for row in lanes if (leading_int(row.cells.get("delegated", "")) or 0) >= 1
    )
    return Computed(
        delegated / len(lanes),
        note=f"{delegated} of {len(lanes)} non-landing lane TERMINALs",
    )


def _share_spec(klass: str, polarity: EnumPolarity) -> MetricSpec:
    return MetricSpec(
        key=f"lanes.share_{klass.replace('-', '_')}",
        label=f"Lanes ended {klass}",
        unit="share of lane TERMINALs",
        kind=EnumMetricKind.RATIO,
        polarity=polarity,
        sources=_LEDGER,
        method="Lane TERMINAL rows by stamp, one per lane and CLAIM, classed by the outcome= cell (landing workers by result=).",
    )


_SHARE_POLARITY = {
    "done": EnumPolarity.HIGHER_IS_BETTER,
    "handed-off": EnumPolarity.HIGHER_IS_BETTER,
    "blocked": EnumPolarity.LOWER_IS_BETTER,
    "failed": EnumPolarity.LOWER_IS_BETTER,
    "partial": EnumPolarity.LOWER_IS_BETTER,
}


def ledger_metrics(ctx: Context, window: Window) -> list[ModelMetric]:
    """Every metric whose only source is the ledger."""
    reads = ctx.reads
    out: list[ModelMetric] = []
    out.append(
        build_metric(
            MetricSpec(
                "validation.refusals",
                "Guard and hook refusals",
                "refusals",
                EnumMetricKind.COUNT,
                EnumPolarity.LOWER_IS_BETTER,
                _LEDGER,
                "FRICTION rows with class=refusal, grouped by their reason= cell.",
            ),
            reads,
            window,
            lambda: _refusals(ctx, window),
        )
    )
    out.append(
        build_metric(
            MetricSpec(
                "validation.preflight_blocks",
                "Hook-preflight blocks",
                "blocks",
                EnumMetricKind.COUNT,
                EnumPolarity.LOWER_IS_BETTER,
                _LEDGER,
                "FRICTION rows whose guard or reason names the preflight marker the contract declares.",
            ),
            reads,
            window,
            lambda: _preflight(ctx, window),
        )
    )
    out.append(
        build_metric(
            MetricSpec(
                "lanes.terminals",
                "Lanes ended",
                "lanes",
                EnumMetricKind.COUNT,
                EnumPolarity.NEUTRAL,
                _LEDGER,
                "Lane TERMINAL rows by stamp, one per lane and CLAIM; the breakdown is the outcome class.",
            ),
            reads,
            window,
            lambda: _terminal_count(ctx, window),
        )
    )
    for klass in sorted(_classes(ctx)):
        polarity = _SHARE_POLARITY.get(
            klass,
            EnumPolarity.LOWER_IS_BETTER
            if klass == ctx.config.landing_failed_class
            else EnumPolarity.NEUTRAL,
        )
        out.append(
            build_metric(
                _share_spec(klass, polarity),
                reads,
                window,
                partial(_share, ctx, window, klass),
            )
        )
    out.append(
        build_metric(
            MetricSpec(
                "andon.stops_started",
                "Andon stops started",
                "stops",
                EnumMetricKind.COUNT,
                EnumPolarity.LOWER_IS_BETTER,
                _LEDGER,
                "HOLD rows by stamp whose reason or text opens with an andon marker the contract declares.",
            ),
            reads,
            window,
            lambda: _andon_stops(ctx, window),
        )
    )
    out.append(
        build_metric(
            MetricSpec(
                "andon.hours_stopped",
                "Hours stopped by an andon",
                "hours",
                EnumMetricKind.HOURS,
                EnumPolarity.LOWER_IS_BETTER,
                _LEDGER,
                "Union of the intervals from an andon HOLD to the earliest of its RELEASE, its until and the generated-at time, clipped to the window.",
            ),
            reads,
            window,
            lambda: _andon_hours(ctx, window),
        )
    )
    out.append(
        build_metric(
            MetricSpec(
                "friction.rows",
                "Friction rows",
                "rows",
                EnumMetricKind.COUNT,
                EnumPolarity.LOWER_IS_BETTER,
                _LEDGER,
                _FRICTION_METHOD,
            ),
            reads,
            window,
            lambda: _friction_rows(ctx, window),
        )
    )
    for bucket, label, method in (
        (
            "lane_stated",
            "Friction minutes stated by lanes",
            "The cost cell of lane-written FRICTION rows: a lane's own estimate, not a measurement.",
        ),
        (
            "reporter_stated",
            "Friction minutes stated by hooks and scripts",
            "The cost cell of FRICTION rows written by a hook, launchd job or script reporter.",
        ),
        (
            "capacity_estimate",
            "Friction minutes that are capacity estimates",
            "The cost cell of rows whose cost opens with an estimate prefix or whose class is a capacity class: idle capacity, never lane time.",
        ),
    ):
        out.append(
            build_metric(
                MetricSpec(
                    f"friction.{bucket}_minutes",
                    label,
                    "minutes",
                    EnumMetricKind.MINUTES,
                    EnumPolarity.LOWER_IS_BETTER,
                    _LEDGER,
                    method,
                ),
                reads,
                window,
                partial(_friction_minutes, ctx, window, bucket),
            )
        )
    out.append(
        build_metric(
            MetricSpec(
                "delegation.lane_share",
                "Lanes that delegated work",
                "share of non-landing lane TERMINALs",
                EnumMetricKind.RATIO,
                EnumPolarity.HIGHER_IS_BETTER,
                _LEDGER,
                "Non-landing lane TERMINALs whose delegated= cell is 1 or more, over all of them.",
            ),
            reads,
            window,
            lambda: _delegation_share(ctx, window),
        )
    )
    return out
