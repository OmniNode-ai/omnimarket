# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The run's measurements, pure: per-arm scores, the p95 latency and the metrics the bar is held against."""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_memory_pilot_effect.handlers.pilot_scoring import (
    ABSTAIN_CATEGORY,
    HARD_CATEGORIES,
    PLANTED_CATEGORY,
)
from omnimarket.nodes.node_memory_pilot_effect.models.model_memory_pilot_run import (
    ModelMemoryPilotArmScore,
    ModelMemoryPilotRunMetricNames,
)


class ModelQuestionTrial(BaseModel):
    """One question asked once of one arm."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    question_id: str
    category: str
    trial: int
    correct: bool
    latency_seconds: float
    errored: bool = False


def _mean(values: Sequence[bool]) -> float:
    return sum(values) / len(values) if values else 0.0


def score_arm(
    arm: str, trials: Sequence[ModelQuestionTrial]
) -> ModelMemoryPilotArmScore:
    planted: dict[str, list[bool]] = defaultdict(list)
    for item in trials:
        if item.category == PLANTED_CATEGORY:
            planted[item.question_id].append(item.correct)
    return ModelMemoryPilotArmScore(
        arm=arm,
        accuracy_overall=_mean([t.correct for t in trials]),
        accuracy_hard=_mean(
            [t.correct for t in trials if t.category in HARD_CATEGORIES]
        ),
        planted_mean=sum(_mean(runs) for runs in planted.values()),
        abstain_mean=_mean(
            [t.correct for t in trials if t.category == ABSTAIN_CATEGORY]
        ),
        queries=len(trials),
        errors=sum(1 for t in trials if t.errored),
    )


def p95_seconds(latencies: Sequence[float]) -> float:
    """Nearest rank: the smallest value at or above 95 percent of the sorted latencies."""
    if not latencies:
        return 0.0
    ordered = sorted(latencies)
    return ordered[math.ceil(0.95 * len(ordered)) - 1]


def run_metrics(
    names: ModelMemoryPilotRunMetricNames,
    baseline: ModelMemoryPilotArmScore,
    candidate: ModelMemoryPilotArmScore,
    *,
    ingest_seconds: float,
    failed_items: int,
    p95: float,
    outside_connections: int,
) -> dict[str, float]:
    return {
        names.overall_gain: candidate.accuracy_overall - baseline.accuracy_overall,
        names.hard_gain: candidate.accuracy_hard - baseline.accuracy_hard,
        names.planted_recalled: candidate.planted_mean,
        names.abstain_margin: candidate.abstain_mean - baseline.abstain_mean,
        names.ingest_seconds: ingest_seconds,
        names.failed_items: float(failed_items),
        names.query_p95_seconds: p95,
        names.outside_connections: float(outside_connections),
    }
