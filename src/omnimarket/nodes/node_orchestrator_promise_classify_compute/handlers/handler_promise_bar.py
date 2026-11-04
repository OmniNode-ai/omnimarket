# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Labelled reply counts, Wilson intervals and a fail-closed measured bar."""

from __future__ import annotations

import math
import re
from collections import Counter
from statistics import NormalDist

from omnimarket.models.model_orchestrator_promise import (
    ModelPromiseBarReport,
    ModelPromiseBarRequest,
    ModelPromiseMetric,
)
from omnimarket.nodes.node_orchestrator_promise_classify_compute.handlers.handler_promise_classify import (
    HandlerPromiseClassify,
    policy_digest,
)


def _metric(successes: int, total: int, confidence: float) -> ModelPromiseMetric:
    if not total:
        return ModelPromiseMetric(successes=0, total=0, estimate=None, lower=0, upper=1)
    z = NormalDist().inv_cdf((1 + confidence) / 2)
    p = successes / total
    denominator = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denominator
    half = (
        z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denominator
    )
    return ModelPromiseMetric(
        successes=successes,
        total=total,
        estimate=p,
        lower=max(0, centre - half),
        upper=min(1, centre + half),
    )


class HandlerPromiseBar:
    def handle(self, request: ModelPromiseBarRequest) -> ModelPromiseBarReport:
        policy = request.policy
        digest = policy_digest(policy)
        ids = [sample.request.utterance.capture_id for sample in request.samples]
        if len(ids) != len(set(ids)):
            raise ValueError("measurement needs distinct source replies")
        tp = fp = fn = 0
        for sample in request.samples:
            if policy_digest(sample.request.policy) != digest:
                raise ValueError("sample policy differs from measured policy")
            result = HandlerPromiseClassify().handle(sample.request)
            expected = Counter(sample.promise_sentences)
            sentences = tuple(
                match.group(0).strip()
                for match in re.finditer(
                    policy.sentence_pattern, sample.request.utterance.text
                )
                if match.group(0).strip()
            )
            if any(index < 1 or index > len(sentences) for index in expected):
                raise ValueError("label does not identify a source sentence")
            predicted = Counter(item.sentence for item in result.items)
            tp += sum((predicted & expected).values())
            fp += sum((predicted - expected).values())
            fn += sum((expected - predicted).values())
        precision = _metric(tp, tp + fp, policy.interval_confidence)
        recall = _metric(tp, tp + fn, policy.interval_confidence)
        reasons: list[str] = []
        if request.origin != "corpus":
            reasons.append("labelled sample is synthetic, not the orchestrator corpus")
        if len(ids) < policy.minimum_replies:
            reasons.append("labelled sample is smaller than the overlay minimum")
        for name, metric, bar in (
            ("precision", precision, policy.minimum_precision),
            ("recall", recall, policy.minimum_recall),
        ):
            statistic = (
                metric.lower
                if policy.bar_statistic == "lower_bound"
                else metric.estimate
            )
            if bar is None:
                reasons.append(f"{name} bar is unset")
            elif statistic is None or statistic < bar:
                reasons.append(f"{name} is below the overlay bar")
        return ModelPromiseBarReport(
            corpus_id=request.corpus_id,
            origin=request.origin,
            labelled_by=request.labelled_by,
            replies=len(ids),
            true_positive=tp,
            false_positive=fp,
            false_negative=fn,
            precision=precision,
            recall=recall,
            interval_confidence=policy.interval_confidence,
            policy_digest=digest,
            passed=not reasons,
            reasons=tuple(reasons),
        )
