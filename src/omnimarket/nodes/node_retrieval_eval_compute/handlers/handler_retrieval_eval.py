# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure retrieval metrics (OMN-21132, plan task TC.9, port item AP-E1)."""

from __future__ import annotations

import math
import random
from collections.abc import Sequence
from functools import lru_cache
from pathlib import Path

import yaml

from omnimarket.models.retrieval_eval import (
    EnumRetrievalMetric,
    EnumRetrievalTargetVerdict,
    ModelRetrievalEvalAggregate,
    ModelRetrievalEvalConfig,
    ModelRetrievalEvalIntervalMethod,
    ModelRetrievalEvalQuery,
    ModelRetrievalEvalQueryScore,
    ModelRetrievalEvalReport,
    ModelRetrievalEvalRequest,
    ModelRetrievalEvalTargetVerdict,
    ModelRetrievalMetricAtK,
    ModelRetrievalMetricSummary,
)

METRICS_VERSION = "retrieval-eval/1"
_CONTRACT = Path(__file__).parents[1] / "contract.yaml"


@lru_cache(maxsize=1)
def retrieval_eval_config() -> ModelRetrievalEvalConfig:
    """Load the immutable evaluation policy from the node contract."""
    data = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    return ModelRetrievalEvalConfig.model_validate(data["config"]["retrieval_eval"])


def recall_at_k(retrieved: Sequence[str], relevant: frozenset[str], k: int) -> float:
    """Fraction of the relevant set found within the first k retrieved items."""
    return len(set(retrieved[:k]) & relevant) / len(relevant)


def precision_at_k(retrieved: Sequence[str], relevant: frozenset[str], k: int) -> float:
    """Relevant hits divided by k, including unfilled retrieval positions."""
    return len(set(retrieved[:k]) & relevant) / k


def average_precision(retrieved: Sequence[str], relevant: frozenset[str]) -> float:
    """Sum precision at relevant ranks, normalized by the relevant set size."""
    hits = 0
    precisions: list[float] = []
    for rank, item in enumerate(retrieved, start=1):
        if item in relevant:
            hits += 1
            precisions.append(hits / rank)
    return math.fsum(precisions) / len(relevant)


def reciprocal_rank(retrieved: Sequence[str], relevant: frozenset[str]) -> float:
    """Reciprocal of the first relevant rank, or zero when no hit exists."""
    for rank, item in enumerate(retrieved, start=1):
        if item in relevant:
            return 1.0 / rank
    return 0.0


def score_query(
    query: ModelRetrievalEvalQuery, k_values: Sequence[int]
) -> ModelRetrievalEvalQueryScore:
    """Score one query; retriever failures contribute zeros to every metric."""
    relevant = frozenset(query.relevant_ids)
    retrieved = query.retrieved_ids
    return ModelRetrievalEvalQueryScore(
        query_id=query.query_id,
        failed=query.failed,
        relevant_count=len(relevant),
        retrieved_count=len(retrieved),
        recall_at_k=tuple(
            ModelRetrievalMetricAtK(
                k=k, value=0.0 if query.failed else recall_at_k(retrieved, relevant, k)
            )
            for k in k_values
        ),
        precision_at_k=tuple(
            ModelRetrievalMetricAtK(
                k=k,
                value=0.0 if query.failed else precision_at_k(retrieved, relevant, k),
            )
            for k in k_values
        ),
        average_precision=0.0
        if query.failed
        else average_precision(retrieved, relevant),
        reciprocal_rank=0.0 if query.failed else reciprocal_rank(retrieved, relevant),
    )


def bootstrap_mean_interval(
    values: Sequence[float], *, interval: ModelRetrievalEvalIntervalMethod
) -> tuple[float, float]:
    """Fixed-seed percentile bootstrap; callers supply query_id-ordered values."""
    n = len(values)
    if not n:
        raise ValueError("a bootstrap needs at least one query")
    rng = random.Random(interval.seed)
    draws = sorted(
        math.fsum(rng.choices(values, k=n)) / n for _ in range(interval.resamples)
    )
    alpha = 1.0 - interval.confidence

    def quantile(q: float) -> float:
        return draws[min(len(draws) - 1, max(0, int(q * len(draws))))]

    return quantile(alpha / 2.0), quantile(1.0 - alpha / 2.0)


def aggregate(
    slice_label: str,
    scores: Sequence[ModelRetrievalEvalQueryScore],
    k_values: Sequence[int],
    interval: ModelRetrievalEvalIntervalMethod,
) -> ModelRetrievalEvalAggregate:
    """Aggregate every query, including failures, in the canonical metric order."""
    ordered = sorted(scores, key=lambda score: score.query_id)
    if not ordered:
        raise ValueError("an aggregate needs at least one query")
    columns: list[tuple[EnumRetrievalMetric, int | None, list[float]]] = []
    for metric in (EnumRetrievalMetric.RECALL_AT_K, EnumRetrievalMetric.PRECISION_AT_K):
        for k in sorted(k_values):
            values = [
                next(
                    item.value
                    for item in (
                        score.recall_at_k
                        if metric is EnumRetrievalMetric.RECALL_AT_K
                        else score.precision_at_k
                    )
                    if item.k == k
                )
                for score in ordered
            ]
            columns.append((metric, k, values))
    columns.extend(
        [
            (
                EnumRetrievalMetric.AVERAGE_PRECISION,
                None,
                [score.average_precision for score in ordered],
            ),
            (
                EnumRetrievalMetric.RECIPROCAL_RANK,
                None,
                [score.reciprocal_rank for score in ordered],
            ),
        ]
    )
    summaries: list[ModelRetrievalMetricSummary] = []
    for metric, cutoff, values in columns:
        low, high = bootstrap_mean_interval(values, interval=interval)
        summaries.append(
            ModelRetrievalMetricSummary(
                metric=metric,
                k=cutoff,
                mean=math.fsum(values) / len(values),
                interval_low=low,
                interval_high=high,
            )
        )
    return ModelRetrievalEvalAggregate(
        slice_label=slice_label,
        query_count=len(ordered),
        failed_count=sum(score.failed for score in ordered),
        metrics=tuple(summaries),
    )


class HandlerRetrievalEval:
    """Evaluate retrieval results using only the contract's metric policy."""

    def __init__(self, config: ModelRetrievalEvalConfig | None = None) -> None:
        self._cfg = config if config is not None else retrieval_eval_config()

    def handle(self, request: ModelRetrievalEvalRequest) -> ModelRetrievalEvalReport:
        cfg = self._cfg
        queries = sorted(request.queries, key=lambda query: query.query_id)
        scores = tuple(score_query(query, cfg.k_values) for query in queries)
        overall = aggregate("all", scores, cfg.k_values, cfg.interval)
        by_slice: dict[str, list[ModelRetrievalEvalQueryScore]] = {}
        for query, score in zip(queries, scores, strict=True):
            for label in query.slices:
                by_slice.setdefault(label, []).append(score)
        observed = next(
            metric.mean
            for metric in overall.metrics
            if metric.metric is cfg.target.metric and metric.k == cfg.target.k
        )
        return ModelRetrievalEvalReport(
            eval_id=request.eval_id,
            metrics_version=METRICS_VERSION,
            k_values=cfg.k_values,
            interval=cfg.interval,
            overall=overall,
            slices=tuple(
                aggregate(label, by_slice[label], cfg.k_values, cfg.interval)
                for label in sorted(by_slice)
            ),
            queries=scores,
            target=ModelRetrievalEvalTargetVerdict(
                target=cfg.target,
                observed_mean=observed,
                verdict=(
                    EnumRetrievalTargetVerdict.PASS
                    if observed >= cfg.target.min_value
                    else EnumRetrievalTargetVerdict.FAIL
                ),
            ),
        )


__all__ = [
    "METRICS_VERSION",
    "HandlerRetrievalEval",
    "aggregate",
    "average_precision",
    "bootstrap_mean_interval",
    "precision_at_k",
    "recall_at_k",
    "reciprocal_rank",
    "retrieval_eval_config",
    "score_query",
]
