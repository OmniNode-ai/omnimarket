# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Session-clustered paired bootstrap, Holm and the noninferiority safeguards. Pure.

Every comparison of ``mod`` with ``stock_rerun`` is paired by boundary and its
interval resamples whole sessions, because boundaries within a session are not
independent. A session is reduced to one vector of sums (per metric: mod
successes, mod trials, stock successes, stock trials; then mod cost and stock
cost) before any resampling, so the resample unit cannot fall below the session
and the order of boundaries cannot matter.

The one-sample routine of ``omnimarket.ranges.bootstrap`` resamples
independent Bernoulli items, which is the independence assumption the clustered
comparison exists to avoid, so it is not used for a comparison here; the
deterministic evaluator-seed convention it states is followed.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from omnimarket.nodes.node_compaction_verdict_compute.models import (
    EnumCompactionVerdict,
    EnumHarmDirection,
    EnumSafeguardStatus,
    ModelCompactionThresholds,
    ModelPairedBoundary,
    ModelPrimaryResult,
    ModelSafeguardResult,
)

__all__ = [
    "COST_RATIO_SAFEGUARD",
    "Evaluation",
    "evaluate",
    "metric_names",
    "rule_of_three_bound",
    "session_vectors",
]

#: The name under which the cost ratio safeguard is reported.
COST_RATIO_SAFEGUARD = "cost_ratio"

_STRIDE = 4


def metric_names(thresholds: ModelCompactionThresholds) -> tuple[str, ...]:
    """Every metric the decision reads, in a fixed order."""
    names = [p.metric for p in thresholds.primaries]
    names += [s.metric for s in thresholds.safeguards]
    return tuple(dict.fromkeys(names))


def rule_of_three_bound(sessions: int) -> float:
    """The session-level rule-of-three upper bound with zero events in both arms."""
    return 3.0 / sessions


def session_vectors(
    boundaries: tuple[ModelPairedBoundary, ...] | list[ModelPairedBoundary],
    metrics: tuple[str, ...],
) -> list[list[float]]:
    """One vector of sums per session, sessions in id order."""
    width = len(metrics) * _STRIDE + 2
    by_session: dict[str, list[float]] = {}
    for cell in boundaries:
        vector = by_session.setdefault(cell.session_id, [0.0] * width)
        for index, metric in enumerate(metrics):
            base = index * _STRIDE
            mod = cell.mod.metrics.get(metric)
            stock = cell.stock.metrics.get(metric)
            if mod is not None:
                vector[base] += mod.successes
                vector[base + 1] += mod.trials
            if stock is not None:
                vector[base + 2] += stock.successes
                vector[base + 3] += stock.trials
        vector[-2] += cell.mod.cost
        vector[-1] += cell.stock.cost
    return [by_session[key] for key in sorted(by_session)]


def _rate(successes: float, trials: float) -> float:
    return successes / trials if trials > 0 else 0.0


@dataclass(frozen=True)
class _Stat:
    kind: str  # "primary" | "safeguard" | "cost"
    index: int
    direction: EnumHarmDirection | None


def _statistic(sums: list[float], stat: _Stat) -> float:
    if stat.kind == "cost":
        return sums[-2] / sums[-1] if sums[-1] > 0 else 0.0
    base = stat.index * _STRIDE
    mod = _rate(sums[base], sums[base + 1])
    stock = _rate(sums[base + 2], sums[base + 3])
    if stat.direction is EnumHarmDirection.LOWER:
        return stock - mod
    return mod - stock


@dataclass(frozen=True)
class Evaluation:
    """The statistics of one corpus; the fallback HOLD is applied by the caller."""

    sessions: int
    primaries: tuple[ModelPrimaryResult, ...]
    safeguards: tuple[ModelSafeguardResult, ...]
    exploratory: bool
    verdict: EnumCompactionVerdict


def _holm(
    p_values: list[tuple[float, str]], alpha: float
) -> dict[str, tuple[float, bool]]:
    """Holm step-down: name -> (threshold, rejected)."""
    ordered = sorted(p_values)
    count = len(ordered)
    out: dict[str, tuple[float, bool]] = {}
    still = True
    for step, (p_value, name) in enumerate(ordered):
        threshold = alpha / (count - step)
        still = still and p_value <= threshold
        out[name] = (threshold, still)
    return out


def _safeguard_status(lower: float, upper: float, margin: float) -> EnumSafeguardStatus:
    if lower > margin:
        return EnumSafeguardStatus.HARM
    if upper < margin:
        return EnumSafeguardStatus.PASSED
    return EnumSafeguardStatus.NOT_ESTABLISHED


def evaluate(
    vectors: list[list[float]],
    thresholds: ModelCompactionThresholds,
    metrics: tuple[str, ...],
    *,
    resamples: int,
    seed: int,
) -> Evaluation:
    sessions = len(vectors)
    if sessions == 0:
        raise ValueError("no scored session to evaluate")
    alpha = thresholds.alpha
    width = len(vectors[0])
    totals = [sum(v[j] for v in vectors) for j in range(width)]

    stats: list[_Stat] = [
        _Stat("primary", metrics.index(p.metric), None) for p in thresholds.primaries
    ]
    stats += [
        _Stat("safeguard", metrics.index(s.metric), s.harm_direction)
        for s in thresholds.safeguards
    ]
    stats.append(_Stat("cost", -1, None))

    rng = random.Random(seed)
    population = range(sessions)
    draws: list[list[float]] = [[] for _ in stats]
    for _ in range(resamples):
        sums = [0.0] * width
        for pick in rng.choices(population, k=sessions):
            row = vectors[pick]
            for j in range(width):
                sums[j] += row[j]
        for slot, stat in enumerate(stats):
            draws[slot].append(_statistic(sums, stat))
    for column in draws:
        column.sort()

    low_at = min(resamples - 1, max(0, int(alpha * resamples)))
    high_at = min(resamples - 1, int((1.0 - alpha) * resamples))

    primary_rows: list[tuple[str, str, float, float, float, float]] = []
    for slot, primary in enumerate(thresholds.primaries):
        column = draws[slot]
        not_positive = sum(1 for d in column if d <= 0.0)
        p_value = (1 + not_positive) / (resamples + 1)
        primary_rows.append(
            (
                primary.name,
                primary.metric,
                _statistic(totals, stats[slot]),
                column[low_at],
                column[high_at],
                p_value,
            )
        )
    holm = _holm([(row[5], row[0]) for row in primary_rows], alpha)
    primaries = tuple(
        ModelPrimaryResult(
            name=name,
            metric=metric,
            estimate=estimate,
            lower=lower,
            upper=upper,
            p_value=p_value,
            holm_threshold=holm[name][0],
            rejected=holm[name][1],
        )
        for name, metric, estimate, lower, upper, p_value in primary_rows
    )

    rule_three = rule_of_three_bound(sessions)
    safeguards: list[ModelSafeguardResult] = []
    exploratory = False
    offset = len(thresholds.primaries)
    for position, spec in enumerate(thresholds.safeguards):
        slot = offset + position
        stat = stats[slot]
        base = stat.index * _STRIDE
        mod_s, mod_t, stock_s, stock_t = totals[base : base + _STRIDE]
        if rule_three >= spec.margin:
            exploratory = True
        if mod_t <= 0 or stock_t <= 0:
            safeguards.append(
                ModelSafeguardResult(
                    name=spec.name,
                    metric=spec.metric,
                    status=EnumSafeguardStatus.NOT_ESTABLISHED,
                    excess=0.0,
                    lower=0.0,
                    upper=1.0,
                    margin=spec.margin,
                    zero_event=False,
                    bound_rule="no_data",
                )
            )
            continue
        if spec.harm_direction is EnumHarmDirection.HIGHER:
            events = mod_s + stock_s
        else:
            events = (mod_t - mod_s) + (stock_t - stock_s)
        if events == 0:
            status = (
                EnumSafeguardStatus.PASSED
                if rule_three < spec.margin
                else EnumSafeguardStatus.NOT_ESTABLISHED
            )
            safeguards.append(
                ModelSafeguardResult(
                    name=spec.name,
                    metric=spec.metric,
                    status=status,
                    excess=0.0,
                    lower=0.0,
                    upper=rule_three,
                    margin=spec.margin,
                    zero_event=True,
                    bound_rule="rule_of_three",
                )
            )
            continue
        column = draws[slot]
        lower, upper = column[low_at], column[high_at]
        safeguards.append(
            ModelSafeguardResult(
                name=spec.name,
                metric=spec.metric,
                status=_safeguard_status(lower, upper, spec.margin),
                excess=_statistic(totals, stat),
                lower=lower,
                upper=upper,
                margin=spec.margin,
                zero_event=False,
                bound_rule="session_bootstrap",
            )
        )

    cost_column = draws[-1]
    if totals[-1] > 0:
        cost_lower, cost_upper = cost_column[low_at], cost_column[high_at]
        limit = thresholds.cost_ratio_limit
        if cost_lower > limit:
            cost_status = EnumSafeguardStatus.HARM
        elif cost_upper <= limit:
            cost_status = EnumSafeguardStatus.PASSED
        else:
            cost_status = EnumSafeguardStatus.NOT_ESTABLISHED
        cost_rule: str = "session_bootstrap"
        cost_estimate = totals[-2] / totals[-1]
    else:
        cost_lower, cost_upper, cost_estimate = 0.0, 0.0, 0.0
        cost_status = EnumSafeguardStatus.NOT_ESTABLISHED
        cost_rule = "no_data"
    safeguards.append(
        ModelSafeguardResult(
            name=COST_RATIO_SAFEGUARD,
            metric="cost",
            status=cost_status,
            excess=cost_estimate,
            lower=cost_lower,
            upper=cost_upper,
            margin=thresholds.cost_ratio_limit,
            zero_event=False,
            bound_rule="session_bootstrap"
            if cost_rule == "session_bootstrap"
            else "no_data",
        )
    )

    established = all(s.status is EnumSafeguardStatus.PASSED for s in safeguards)
    band = thresholds.equivalence_band
    if any(s.status is EnumSafeguardStatus.HARM for s in safeguards):
        verdict = EnumCompactionVerdict.HARM
    elif (
        not exploratory
        and established
        and any(p.rejected and p.lower > 0.0 for p in primaries)
    ):
        verdict = EnumCompactionVerdict.HELPS
    elif (
        not exploratory
        and established
        and all(-band < p.lower and p.upper < band for p in primaries)
    ):
        verdict = EnumCompactionVerdict.NO_DIFFERENCE
    else:
        verdict = EnumCompactionVerdict.INCONCLUSIVE
    return Evaluation(
        sessions=sessions,
        primaries=primaries,
        safeguards=tuple(safeguards),
        exploratory=exploratory,
        verdict=verdict,
    )
