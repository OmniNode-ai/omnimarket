# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Joint power simulation of the whole compaction decision. Pure.

``omnimarket.ranges.power`` sizes a one-sample binomial line only. The
compaction decision is a paired, session-clustered comparison with a Holm step
over the primaries and a set of noninferiority safeguards, so its power is
simulated, not computed in closed form:

* the corpus shape is the real one: the same sessions, the same boundaries per
  session and the same item counts per boundary and metric;
* session clustering: every session draws one shift from a normal with the
  pre-registered ``power_session_sd`` that moves every rate of that session,
  in both arms, and a second draw with the same spread that moves the mod's
  gain on the primaries in that session, so the paired difference itself is
  correlated within a session (a session where the mod helps less helps less
  at every boundary);
* pairing by boundary: the two arms of one item share one uniform draw, so an
  item stock gets right the mod gets right too unless the assumed gain moves it;
* the stock rate of each metric is its pooled observed stock rate; the mod
  rate adds the pre-registered ``power_assumed_gain`` to each primary and
  equals stock on every safeguard (no harm), and cost is equal in both arms;
* each simulated corpus is run through :func:`verdict_stats.evaluate`, the same
  session bootstrap, Holm step and every safeguard (zero-event rule and
  feasibility included) as the real run, and power is the share of simulated
  corpora that return ``HELPS``.
"""

from __future__ import annotations

import random

from omnimarket.nodes.node_compaction_verdict_compute.handlers.verdict_stats import (
    COST_RATIO_SAFEGUARD,
    evaluate,
    metric_names,
)
from omnimarket.nodes.node_compaction_verdict_compute.models import (
    EnumCompactionVerdict,
    ModelCompactionThresholds,
    ModelJointPower,
    ModelPairedBoundary,
)

__all__ = ["simulate_joint_power"]

_POWER_SEED = 20261103


def _clip(value: float) -> float:
    return min(1.0, max(0.0, value))


def simulate_joint_power(
    boundaries: tuple[ModelPairedBoundary, ...] | list[ModelPairedBoundary],
    thresholds: ModelCompactionThresholds,
) -> ModelJointPower:
    """Simulated probability that the decision returns HELPS at this corpus shape."""
    metrics = metric_names(thresholds)
    gains = {p.metric: p.power_assumed_gain for p in thresholds.primaries}
    ordered = sorted(boundaries, key=lambda c: (c.session_id, c.boundary_id))
    shape: dict[str, list[tuple[list[int], float]]] = {}
    pooled = {m: [0, 0] for m in metrics}
    for cell in ordered:
        trials: list[int] = []
        for metric in metrics:
            count = cell.stock.metrics.get(metric)
            trials.append(count.trials if count is not None else 0)
            if count is not None:
                pooled[metric][0] += count.successes
                pooled[metric][1] += count.trials
        shape.setdefault(cell.session_id, []).append((trials, cell.stock.cost))
    base = [pooled[m][0] / pooled[m][1] if pooled[m][1] else 0.0 for m in metrics]
    gain = [gains.get(m, 0.0) for m in metrics]
    sessions = [shape[key] for key in sorted(shape)]
    width = len(metrics) * 4 + 2

    rng = random.Random(_POWER_SEED)
    helps = 0
    for simulation in range(thresholds.power_simulations):
        vectors: list[list[float]] = []
        for session in sessions:
            shift = rng.gauss(0.0, thresholds.power_session_sd)
            spread = rng.gauss(0.0, thresholds.power_session_sd)
            stock_p = [_clip(b + shift) for b in base]
            mod_p = [
                _clip(b + shift + (g + spread if g else 0.0))
                for b, g in zip(base, gain, strict=True)
            ]
            vector = [0.0] * width
            for trials, cost in session:
                for index, items in enumerate(trials):
                    offset = index * 4
                    for _ in range(items):
                        draw = rng.random()
                        vector[offset] += draw < mod_p[index]
                        vector[offset + 2] += draw < stock_p[index]
                    vector[offset + 1] += items
                    vector[offset + 3] += items
                vector[-2] += cost
                vector[-1] += cost
            vectors.append(vector)
        outcome = evaluate(
            vectors,
            thresholds,
            metrics,
            resamples=thresholds.power_resamples,
            seed=_POWER_SEED + simulation,
        )
        helps += outcome.verdict is EnumCompactionVerdict.HELPS
    return ModelJointPower(
        simulated_power=helps / thresholds.power_simulations,
        simulations=thresholds.power_simulations,
        resamples=thresholds.power_resamples,
        sessions=len(sessions),
        boundaries=len(ordered),
        session_sd=thresholds.power_session_sd,
        assumed_gains=gains,
        minimum_power=thresholds.minimum_power,
        minimum_sessions=thresholds.minimum_sessions,
        models_session_clustering=True,
        models_boundary_pairing=True,
        models_holm=True,
        safeguards_modelled=(
            *(s.name for s in thresholds.safeguards),
            COST_RATIO_SAFEGUARD,
        ),
    )
