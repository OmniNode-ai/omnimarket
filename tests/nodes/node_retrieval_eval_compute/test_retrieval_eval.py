# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Retrieval metric regressions (OMN-21132, plan task TC.9, port item AP-E1)."""

from __future__ import annotations

import math
import random

import pytest
from pydantic import ValidationError

from omnimarket.models.retrieval_eval import (
    EnumRetrievalMetric,
    EnumRetrievalTargetVerdict,
    ModelRetrievalEvalAggregate,
    ModelRetrievalEvalConfig,
    ModelRetrievalEvalIntervalMethod,
    ModelRetrievalEvalQuery,
    ModelRetrievalEvalRequest,
    ModelRetrievalEvalTarget,
    ModelRetrievalMetricSummary,
)
from omnimarket.nodes.node_retrieval_eval_compute.handlers.handler_retrieval_eval import (
    METRICS_VERSION,
    HandlerRetrievalEval,
    aggregate,
    average_precision,
    bootstrap_mean_interval,
    precision_at_k,
    recall_at_k,
    reciprocal_rank,
    score_query,
)

pytestmark = pytest.mark.unit


def _interval() -> ModelRetrievalEvalIntervalMethod:
    return ModelRetrievalEvalIntervalMethod(
        method="bootstrap_percentile", confidence=0.95, resamples=200, seed=20261018
    )


def _config(min_value: float = 0.8) -> ModelRetrievalEvalConfig:
    return ModelRetrievalEvalConfig(
        k_values=(1, 3, 5, 10),
        target=ModelRetrievalEvalTarget(
            metric=EnumRetrievalMetric.RECALL_AT_K, k=1, min_value=min_value
        ),
        interval=_interval(),
    )


def _summary(
    result: ModelRetrievalEvalAggregate,
    metric: EnumRetrievalMetric = EnumRetrievalMetric.RECALL_AT_K,
    k: int | None = 1,
) -> ModelRetrievalMetricSummary:
    return next(
        item for item in result.metrics if item.metric is metric and item.k == k
    )


def _mixed_queries() -> tuple[ModelRetrievalEvalQuery, ...]:
    return (
        ModelRetrievalEvalQuery(
            query_id="q1", relevant_ids=("a",), retrieved_ids=("a",)
        ),
        ModelRetrievalEvalQuery(
            query_id="q2", relevant_ids=("b",), failed=True, failure_reason="timeout"
        ),
    )


def test_recall_with_fewer_than_k_retrieved_is_not_zero() -> None:
    relevant = frozenset({"a", "b"})
    assert recall_at_k(("a",), relevant, 10) == 0.5
    assert recall_at_k(("a",), relevant, 1) == 0.5


def test_recall_divides_by_the_relevant_set() -> None:
    relevant = frozenset({"a", "b", "c", "d"})
    assert recall_at_k(("a", "x", "b"), relevant, 3) == 0.5
    assert precision_at_k(("a", "x", "b"), relevant, 3) == 2 / 3


def test_precision_divisor_is_k() -> None:
    assert precision_at_k(("a",), frozenset({"a"}), 5) == 0.2


def test_average_precision_and_reciprocal_rank_golden() -> None:
    relevant = frozenset({"a", "c"})
    retrieved = ("x", "a", "y", "c")
    assert average_precision(retrieved, relevant) == (1 / 2 + 2 / 4) / 2 == 0.5
    assert reciprocal_rank(retrieved, relevant) == 0.5
    assert average_precision(("x", "y"), relevant) == 0.0
    assert reciprocal_rank(("x", "y"), relevant) == 0.0
    assert average_precision(("a",), relevant) == 0.5
    assert average_precision((), relevant) == reciprocal_rank((), relevant) == 0.0


def test_failed_query_counts_in_the_denominator() -> None:
    report = HandlerRetrievalEval(_config()).handle(
        ModelRetrievalEvalRequest(eval_id="failures", queries=_mixed_queries())
    )
    assert report.overall.query_count == 2
    assert report.overall.failed_count == 1
    assert _summary(report.overall).mean == 0.5
    assert (
        _summary(report.overall, EnumRetrievalMetric.RECIPROCAL_RANK, None).mean == 0.5
    )
    failed = report.queries[1]
    assert failed.failed
    assert failed.retrieved_count == 0
    assert failed.average_precision == failed.reciprocal_rank == 0.0
    assert all(
        item.value == 0.0 for item in (*failed.recall_at_k, *failed.precision_at_k)
    )
    assert (
        _summary(report.overall, EnumRetrievalMetric.AVERAGE_PRECISION, None).mean
        == 0.5
    )
    assert _summary(report.overall, EnumRetrievalMetric.PRECISION_AT_K, 1).mean == 0.5


def test_target_comes_from_the_contract() -> None:
    request = ModelRetrievalEvalRequest(eval_id="target", queries=_mixed_queries())
    passed = HandlerRetrievalEval(_config(0.4)).handle(request)
    failed = HandlerRetrievalEval(_config(0.9)).handle(request)
    assert passed.target.verdict is EnumRetrievalTargetVerdict.PASS
    assert failed.target.verdict is EnumRetrievalTargetVerdict.FAIL
    assert passed.target.observed_mean == failed.target.observed_mean == 0.5
    assert "target" not in ModelRetrievalEvalRequest.model_fields
    assert set(ModelRetrievalEvalRequest.model_fields) == {"eval_id", "queries"}
    assert (
        HandlerRetrievalEval(_config(0.5)).handle(request).target.verdict
        is EnumRetrievalTargetVerdict.PASS
    )


def test_per_slice_aggregation() -> None:
    queries = (
        ModelRetrievalEvalQuery(
            query_id="q1",
            relevant_ids=("a",),
            retrieved_ids=("a",),
            slices=("profile:b", "profile:a"),
        ),
        ModelRetrievalEvalQuery(
            query_id="q2",
            relevant_ids=("a",),
            failed=True,
            failure_reason="timeout",
            slices=("profile:a",),
        ),
        ModelRetrievalEvalQuery(query_id="q3", relevant_ids=("a",)),
    )
    report = HandlerRetrievalEval(_config()).handle(
        ModelRetrievalEvalRequest(eval_id="slices", queries=queries)
    )
    assert report.overall.slice_label == "all"
    assert report.overall.query_count == 3
    assert _summary(report.overall).mean == 1 / 3
    assert tuple(item.slice_label for item in report.slices) == (
        "profile:a",
        "profile:b",
    )
    a, b = report.slices
    assert (a.query_count, a.failed_count, _summary(a).mean) == (2, 1, 0.5)
    assert (b.query_count, b.failed_count, _summary(b).mean) == (1, 0, 1.0)


def test_report_is_deterministic_and_order_independent() -> None:
    queries = (
        ModelRetrievalEvalQuery(
            query_id="z",
            relevant_ids=("a",),
            retrieved_ids=("x", "a"),
            slices=("b", "a"),
        ),
        ModelRetrievalEvalQuery(
            query_id="a",
            relevant_ids=("a",),
            retrieved_ids=("a",),
            slices=("a",),
        ),
    )
    handler = HandlerRetrievalEval(_config())
    request = ModelRetrievalEvalRequest(eval_id="determinism", queries=queries)
    report = handler.handle(request)
    reordered = ModelRetrievalEvalRequest(
        eval_id=request.eval_id, queries=queries[::-1]
    )
    assert report.model_dump_json() == handler.handle(request).model_dump_json()
    assert report.model_dump_json() == handler.handle(reordered).model_dump_json()
    assert tuple(score.query_id for score in report.queries) == ("a", "z")
    nonconstant = _summary(report.overall)
    assert nonconstant.interval_low < nonconstant.interval_high
    assert nonconstant.interval_low <= nonconstant.mean <= nonconstant.interval_high
    constant = _summary(report.overall, k=3)
    assert constant.interval_low == constant.interval_high == constant.mean == 1.0
    assert report.metrics_version == METRICS_VERSION == "retrieval-eval/1"
    assert tuple((item.metric, item.k) for item in report.overall.metrics) == (
        *((EnumRetrievalMetric.RECALL_AT_K, k) for k in (1, 3, 5, 10)),
        *((EnumRetrievalMetric.PRECISION_AT_K, k) for k in (1, 3, 5, 10)),
        (EnumRetrievalMetric.AVERAGE_PRECISION, None),
        (EnumRetrievalMetric.RECIPROCAL_RANK, None),
    )


@pytest.mark.parametrize(
    "bad_input",
    [
        {"relevant_ids": ()},
        {"relevant_ids": ("a", "a")},
        {"retrieved_ids": ("a", "a")},
        {"failed": True, "retrieved_ids": ("a",), "failure_reason": "timeout"},
        {"failed": True},
        {"failed": True, "failure_reason": ""},
        {"failed": True, "failure_reason": "   "},
        {"failure_reason": "timeout"},
        {"slices": ("profile:a", "profile:a")},
        {"query_id": ""},
        {"duplicate_query_id": True},
    ],
)
def test_query_validation_refuses_bad_input(bad_input: dict[str, object]) -> None:
    if "duplicate_query_id" in bad_input:
        query = ModelRetrievalEvalQuery(query_id="q", relevant_ids=("a",))
        with pytest.raises(ValidationError):
            ModelRetrievalEvalRequest(eval_id="duplicate", queries=(query, query))
    else:
        with pytest.raises(ValidationError):
            ModelRetrievalEvalQuery.model_validate(
                {"query_id": "q", "relevant_ids": ("a",), **bad_input}
            )


@pytest.mark.parametrize(
    ("k_values", "metric", "k"),
    [
        ((1, 3), EnumRetrievalMetric.RECALL_AT_K, 10),
        ((3, 1), EnumRetrievalMetric.RECALL_AT_K, 1),
        ((1, 1), EnumRetrievalMetric.RECALL_AT_K, 1),
        ((0, 1), EnumRetrievalMetric.RECALL_AT_K, 1),
        ((), EnumRetrievalMetric.RECALL_AT_K, 1),
        ((1, 3), EnumRetrievalMetric.AVERAGE_PRECISION, 1),
        ((1, 3), EnumRetrievalMetric.RECIPROCAL_RANK, 1),
        ((1, 3), EnumRetrievalMetric.RECALL_AT_K, None),
        ((1, 3), EnumRetrievalMetric.PRECISION_AT_K, None),
        ((1, 3), EnumRetrievalMetric.PRECISION_AT_K, 0),
    ],
)
def test_config_validation(
    k_values: tuple[int, ...], metric: EnumRetrievalMetric, k: int | None
) -> None:
    with pytest.raises(ValidationError):
        ModelRetrievalEvalConfig(
            k_values=k_values,
            target=ModelRetrievalEvalTarget(metric=metric, k=k, min_value=0.8),
            interval=_interval(),
        )


@pytest.mark.parametrize("confidence", [0.6, 0.95])
@pytest.mark.parametrize("seed", [42, 20261018])
def test_bootstrap_matches_fixed_seed_percentile_rule(
    confidence: float, seed: int
) -> None:
    interval = ModelRetrievalEvalIntervalMethod(
        method="bootstrap_percentile", confidence=confidence, resamples=123, seed=seed
    )
    values = (0.0, 0.25, 0.5, 0.75)
    rng = random.Random(seed)
    draws = sorted(math.fsum(rng.choices(values, k=4)) / 4 for _ in range(123))
    alpha = 1.0 - confidence
    expected = tuple(
        draws[min(122, max(0, int(q * 123)))] for q in (alpha / 2, 1 - alpha / 2)
    )
    assert bootstrap_mean_interval(values, interval=interval) == expected
    assert bootstrap_mean_interval((0.25,), interval=interval) == (0.25, 0.25)
    with pytest.raises(ValueError, match="at least one query"):
        bootstrap_mean_interval((), interval=interval)


def test_public_aggregate_sorts_queries_and_retains_failures() -> None:
    cfg = _config()
    scores = tuple(score_query(query, cfg.k_values) for query in _mixed_queries())
    result = aggregate("slice", scores, cfg.k_values, cfg.interval)
    assert result == aggregate("slice", scores[::-1], cfg.k_values, cfg.interval)
    assert result.query_count == 2
    assert result.failed_count == 1
    assert _summary(result).mean == 0.5
    with pytest.raises(ValueError, match="at least one query"):
        aggregate("empty", (), cfg.k_values, cfg.interval)


@pytest.mark.parametrize(
    ("metric", "k", "expected"),
    [
        (EnumRetrievalMetric.PRECISION_AT_K, 2, 0.5),
        (EnumRetrievalMetric.AVERAGE_PRECISION, None, 1.0),
        (EnumRetrievalMetric.RECIPROCAL_RANK, None, 1.0),
    ],
)
def test_custom_contract_policy_controls_cutoffs_and_target(
    metric: EnumRetrievalMetric, k: int | None, expected: float
) -> None:
    cfg = ModelRetrievalEvalConfig(
        k_values=(2, 4),
        target=ModelRetrievalEvalTarget(metric=metric, k=k, min_value=expected),
        interval=_interval(),
    )
    report = HandlerRetrievalEval(cfg).handle(
        ModelRetrievalEvalRequest(eval_id="policy", queries=(_mixed_queries()[0],))
    )
    assert report.k_values == (2, 4)
    assert report.interval == cfg.interval
    assert report.target.target == cfg.target
    assert report.target.observed_mean == expected
    assert report.target.verdict is EnumRetrievalTargetVerdict.PASS
