# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""TE.5 adoption over recorded round-one results, including Role B (OMN-21142).

No I/O, clock, or model calls: every case, repeat, and measurement comes from the request.
The hard gate requires complete repeat coverage, full recall on confirmed lost-ruling cases,
no credited negative controls or duplicate work, and aggregate recall and next-action rates
at least as good as the specified references. Comparisons use exact fractions: recall is
weighted by labelled items after averaging each case's repeats; next-action rates give each
case equal weight. Failures name cases and sort in EnumGateMetric declaration order.

Parse failures and Role B tail-summary matches are reported and never gate. Passing rungs
compete by measured cost, then total tokens; ties all receive routing-contract promotions
for the role's task class in a separate PR. No profile is edited. TE.7 cascade justification
uses the first sorted cheapest rung and only positive, caller-supplied thresholds.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from math import fsum

from omnimarket.nodes.node_context_role_adoption_verdict_compute.models.model_role_adoption_verdict import (
    EnumAdoptionOutcome,
    EnumGateMetric,
    ModelEvalCase,
    ModelGateFailure,
    ModelLatency,
    ModelRoleAdoptionVerdictRequest,
    ModelRoleAdoptionVerdictResult,
    ModelRoutingPromotion,
    ModelRungCost,
    ModelRungEvaluation,
    ModelRungFailed,
    ModelRungPassed,
)


@dataclass(frozen=True)
class _Scores:
    recall_by_case: dict[str, Fraction]
    action_by_case: dict[str, Fraction]
    recall: Fraction
    action: Fraction


def _scores(
    evaluation: ModelRungEvaluation, cases: tuple[ModelEvalCase, ...]
) -> _Scores:
    recall_by_case: dict[str, Fraction] = {}
    action_by_case: dict[str, Fraction] = {}
    weighted_recall = Fraction(0)
    for case in cases:
        runs = tuple(run for run in evaluation.runs if run.case_id == case.case_id)
        if not runs:
            # Missing cases fail coverage; comparison failures only name recorded cases.
            continue
        labels = set(case.labelled_item_ids)
        recall = sum(
            (
                Fraction(len(set(run.recalled_item_ids) & labels), len(labels))
                for run in runs
            ),
            Fraction(0),
        ) / len(runs)
        recall_by_case[case.case_id] = recall
        action_by_case[case.case_id] = Fraction(
            sum(run.next_action_correct for run in runs), len(runs)
        )
        weighted_recall += recall * len(labels)
    return _Scores(
        recall_by_case=recall_by_case,
        action_by_case=action_by_case,
        recall=weighted_recall / sum(len(case.labelled_item_ids) for case in cases),
        action=sum(action_by_case.values(), Fraction(0)) / len(cases),
    )


def _cost(rung: ModelRungEvaluation) -> ModelRungCost:
    latencies = sorted(run.latency_ms for run in rung.runs)
    count = len(latencies)
    latency = ModelLatency(
        p50_ms=latencies[(count * 50 + 99) // 100 - 1] if count else 0,
        p95_ms=latencies[(count * 95 + 99) // 100 - 1] if count else 0,
        max_ms=latencies[-1] if count else 0,
    )
    return ModelRungCost(
        tokens_in=sum(run.tokens_in for run in rung.runs),
        tokens_out=sum(run.tokens_out for run in rung.runs),
        cost_usd=fsum(run.cost_usd for run in rung.runs),
        latency=latency,
    )


def _failures(
    request: ModelRoleAdoptionVerdictRequest,
    rung: ModelRungEvaluation,
    scores: _Scores,
    baseline: _Scores,
    control: _Scores,
) -> tuple[ModelGateFailure, ...]:
    failures: list[ModelGateFailure] = []
    recorded = {(run.case_id, run.repeat) for run in rung.runs}
    cases = {case.case_id: case for case in request.cases}
    for case in request.cases:
        for repeat in range(1, request.repeats_required + 1):
            if (case.case_id, repeat) not in recorded:
                failures.append(
                    ModelGateFailure(
                        metric=EnumGateMetric.COVERAGE,
                        case_id=case.case_id,
                        repeat=repeat,
                        detail="no recorded run",
                    )
                )
    for run in rung.runs:
        case = cases[run.case_id]
        recalled = set(run.recalled_item_ids)
        violations = (
            (
                EnumGateMetric.RULING_RECALL,
                set(case.labelled_item_ids) - recalled
                if case.confirmed_lost_ruling
                else set(),
                "missing ",
            ),
            (
                EnumGateMetric.NEGATIVE_CONTROL,
                recalled & set(case.negative_control_ids),
                "credited ",
            ),
            (
                EnumGateMetric.DUPLICATE_WORK,
                set(run.proposed_item_ids) & set(case.already_done_item_ids),
                "proposes already-done ",
            ),
        )
        for metric, item_ids, prefix in violations:
            if item_ids:
                failures.append(
                    ModelGateFailure(
                        metric=metric,
                        case_id=run.case_id,
                        repeat=run.repeat,
                        detail=prefix + ",".join(sorted(item_ids)),
                    )
                )
    comparisons = (
        (
            EnumGateMetric.RECALL_BELOW_BASELINE,
            scores.recall,
            baseline.recall,
            scores.recall_by_case,
            baseline.recall_by_case,
            "baseline",
        ),
        (
            EnumGateMetric.NEXT_ACTION_BELOW_CONTROL,
            scores.action,
            control.action,
            scores.action_by_case,
            control.action_by_case,
            "control",
        ),
        (
            EnumGateMetric.NEXT_ACTION_BELOW_BASELINE,
            scores.action,
            baseline.action,
            scores.action_by_case,
            baseline.action_by_case,
            "baseline",
        ),
    )
    for metric, aggregate, reference, per_case, reference_cases, name in comparisons:
        if aggregate < reference:
            for case_id, value in per_case.items():
                if value < reference_cases[case_id]:
                    failures.append(
                        ModelGateFailure(
                            metric=metric,
                            case_id=case_id,
                            repeat=None,
                            detail=f"rung {value} below {name} {reference_cases[case_id]}",
                        )
                    )
    order = {metric: index for index, metric in enumerate(EnumGateMetric)}
    return tuple(
        sorted(
            failures,
            key=lambda failure: (
                order[failure.metric],
                failure.case_id,
                failure.repeat if failure.repeat is not None else 0,
            ),
        )
    )


class HandlerRoleAdoptionVerdict:
    """Deterministic role adoption: ``handle(request) -> result``."""

    def handle(
        self, request: ModelRoleAdoptionVerdictRequest
    ) -> ModelRoleAdoptionVerdictResult:
        baseline = _scores(request.baseline, request.cases)
        control = _scores(request.control_without_events, request.cases)
        passed: list[ModelRungPassed] = []
        failed: list[ModelRungFailed] = []
        for rung in sorted(request.rungs, key=lambda rung: (rung.rung_id, rung.mode)):
            scores = _scores(rung, request.cases)
            failures = _failures(request, rung, scores, baseline, control)
            parse_failures = sum(run.parse_failures for run in rung.runs)
            if failures:
                failed.append(
                    ModelRungFailed(
                        rung_id=rung.rung_id,
                        served_model_id=rung.served_model_id,
                        mode=rung.mode,
                        failures=failures,
                        parse_failures=parse_failures,
                    )
                )
            else:
                passed.append(
                    ModelRungPassed(
                        rung_id=rung.rung_id,
                        served_model_id=rung.served_model_id,
                        mode=rung.mode,
                        cost=_cost(rung),
                        parse_failures=parse_failures,
                        recall=float(scores.recall),
                        tail_summary_scored=sum(
                            run.tail_summary_matches is not None for run in rung.runs
                        ),
                        tail_summary_matched=sum(
                            run.tail_summary_matches is True for run in rung.runs
                        ),
                    )
                )
        cheapest: tuple[ModelRungPassed, ...] = ()
        if passed:
            minimum = min(
                (rung.cost.cost_usd, rung.cost.tokens_in + rung.cost.tokens_out)
                for rung in passed
            )
            cheapest = tuple(
                rung
                for rung in passed
                if (rung.cost.cost_usd, rung.cost.tokens_in + rung.cost.tokens_out)
                == minimum
            )
        # Sort the public keys explicitly: rung-id prefixes can change string ordering.
        lowest_cost = tuple(sorted(f"{rung.rung_id}:{rung.mode}" for rung in cheapest))
        promotions = tuple(
            ModelRoutingPromotion(
                task_class=request.task_class,
                rung_id=rung.rung_id,
                served_model_id=rung.served_model_id,
                mode=rung.mode,
            )
            for rung in sorted(cheapest, key=lambda rung: f"{rung.rung_id}:{rung.mode}")
        )
        if promotions:
            selected = next(
                rung
                for rung in cheapest
                if f"{rung.rung_id}:{rung.mode}" == lowest_cost[0]
            )
            cost = selected.cost
            justified = (
                request.cascade_min_tokens_in > 0
                and cost.tokens_in >= request.cascade_min_tokens_in
            ) or (
                request.cascade_min_cost_usd > 0
                and cost.cost_usd >= request.cascade_min_cost_usd
            )
            cascade_reason = (
                f"tokens_in={cost.tokens_in}, cost_usd={cost.cost_usd}; "
                f"cascade_min_tokens_in={request.cascade_min_tokens_in}, "
                f"cascade_min_cost_usd={request.cascade_min_cost_usd}: "
                f"{'justified' if justified else 'not justified'}"
            )
            recommendation = (
                f"promote {', '.join(lowest_cost)} for task class {request.task_class} "
                "by adding a routing-contract entry in a separate PR; no profile is edited"
            )
        else:
            justified = False
            cascade_reason = "no rung passed the hard gate, so there is no extractor to put a gate in front of"
            recommendation = (
                "no rung met the hard gate: adopt the deterministic package without model events; "
                "TA.6 and TA.8 stay unchanged"
            )
        return ModelRoleAdoptionVerdictResult(
            evaluation_id=request.evaluation_id,
            task_class=request.task_class,
            outcome=EnumAdoptionOutcome.ADOPT
            if passed
            else EnumAdoptionOutcome.NO_RUNG_PASSED,
            passed=tuple(passed),
            failed=tuple(failed),
            lowest_cost=lowest_cost,
            promotions=promotions,
            recommendation=recommendation,
            cascade_justified=justified,
            cascade_reason=cascade_reason,
        )
