# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Pure Role C replay scoring beside the existing context selection policy.

The caller supplies pinned labels, eligible candidates and recorded selections.
This operation does not run rungs or promote them. The baseline shares the
selection policy's score/source ordering and greedily fills the manifest budget,
with required items protected. Optional alternatives remain unmeasured.
"""

from __future__ import annotations

from itertools import product
from typing import Literal

from omnimarket.nodes.node_context_selection_policy_compute.handlers.handler_context_selection_policy import (
    _rank_key,
)
from omnimarket.nodes.node_context_selection_policy_compute.models.model_selection_evaluation import (
    ModelSelectionEvaluationCase,
    ModelSelectionEvaluationRequest,
    ModelSelectionEvaluationResult,
    ModelSelectionEvaluationRow,
    ModelSelectionEvaluationSummary,
    ModelSelectionObservation,
    ModelSelectionRecommendation,
)

_REPEATS: tuple[Literal[1, 2, 3], ...] = (1, 2, 3)
_BASELINE = "baseline"


def _required(case: ModelSelectionEvaluationCase) -> set[str]:
    factors = set(case.resolved_required_factors)
    return {
        item.candidate.source
        for item in case.items
        if item.candidate.is_required or item.candidate.factor in factors
    }


def _case_errors(case: ModelSelectionEvaluationCase) -> list[str]:
    errors: list[str] = []
    required = _required(case)
    sources = [item.candidate.source for item in case.items]
    labels = set(case.relevant_optional_sources)
    withheld = set(case.withheld_relevant_sources)
    if len(sources) != len(set(sources)):
        errors.append("duplicate candidate source")
    if len(labels) != len(case.relevant_optional_sources) or any(not s for s in labels):
        errors.append("empty or duplicate relevant label")
    if len(withheld) != len(case.withheld_relevant_sources):
        errors.append("duplicate withheld label")
    if not withheld <= labels or withheld & set(sources):
        errors.append("control must withhold labelled sources from candidates")
    if labels - set(sources) != withheld:
        errors.append("absent relevant sources must be declared withheld")
    if labels & required:
        errors.append("relevant labels must describe optional items")
    available_factors = {item.candidate.factor for item in case.items}
    if not set(case.resolved_required_factors) <= available_factors:
        errors.append("resolved required factor missing from candidates")
    if (
        sum(i.token_estimate for i in case.items if i.candidate.source in required)
        > case.token_budget
    ):
        errors.append("required candidates exceed token budget")
    return [f"{case.profile_id}/{case.case_id}: {error}" for error in errors]


def _baseline(case: ModelSelectionEvaluationCase) -> tuple[str, ...]:
    required = _required(case)
    selected = [
        i.candidate.source for i in case.items if i.candidate.source in required
    ]
    used = sum(i.token_estimate for i in case.items if i.candidate.source in required)
    optional_count = 0
    for item in sorted(case.items, key=lambda i: _rank_key(i.candidate)):
        if item.candidate.source in required:
            continue
        if (
            optional_count < case.max_optional
            and used + item.token_estimate <= case.token_budget
        ):
            selected.append(item.candidate.source)
            optional_count += 1
            used += item.token_estimate
    # Required items have no model ordering; source order makes replay independent
    # of the candidate enumeration in the source reader.
    return tuple(sorted(required)) + tuple(s for s in selected if s not in required)


def _observation_errors(
    case: ModelSelectionEvaluationCase, observation: ModelSelectionObservation
) -> list[str]:
    selected = set(observation.selected_sources)
    items = {i.candidate.source: i for i in case.items}
    errors: list[str] = []
    if len(selected) != len(observation.selected_sources):
        errors.append("duplicate selected source")
    if not selected <= items.keys():
        errors.append("unknown selected source")
    if observation.status != "ok":
        if selected:
            errors.append("failed observation may not claim a package")
    else:
        if not _required(case) <= selected:
            errors.append("package omits a resolved required factor or required item")
        if len(selected - _required(case)) > case.max_optional:
            errors.append("package exceeds optional item limit")
        if (
            sum(items[s].token_estimate for s in selected if s in items)
            > case.token_budget
        ):
            errors.append("package exceeds token budget")
    prefix = f"{case.profile_id}/{case.case_id}/{observation.rung}/{observation.repeat}"
    return [f"{prefix}: {error}" for error in errors]


def _score(
    case: ModelSelectionEvaluationCase, observation: ModelSelectionObservation
) -> ModelSelectionEvaluationRow:
    labels = set(case.relevant_optional_sources)
    selected = set(observation.selected_sources)
    recalled = len(labels & selected)
    return ModelSelectionEvaluationRow(
        profile_id=case.profile_id,
        case_id=case.case_id,
        manifest_hash=case.manifest_hash,
        rung=observation.rung,
        repeat=observation.repeat,
        status=observation.status,
        positive_control=bool(case.withheld_relevant_sources),
        selected_sources=observation.selected_sources,
        missed_sources=tuple(sorted(labels - selected)),
        relevant_count=len(labels),
        recalled_count=recalled,
        recall=recalled / len(labels),
        package_tokens=(
            sum(i.token_estimate for i in case.items if i.candidate.source in selected)
            if observation.status == "ok"
            else None
        ),
        prompt_tokens=observation.prompt_tokens,
        completion_tokens=observation.completion_tokens,
    )


class HandlerContextSelectionEvaluation:
    """Score a complete three-repeat matrix, refusing invalid recorded packages."""

    def handle(
        self, request: ModelSelectionEvaluationRequest
    ) -> ModelSelectionEvaluationResult:
        errors: list[str] = []
        profiles, rungs = set(request.profile_ids), set(request.rungs)
        if len(profiles) != len(request.profile_ids) or any(not p for p in profiles):
            errors.append("empty or duplicate profile id")
        if (
            len(rungs) != len(request.rungs)
            or _BASELINE in rungs
            or any(not r for r in rungs)
        ):
            errors.append("empty, duplicate or reserved rung id")
        cases = {(c.profile_id, c.case_id): c for c in request.cases}
        if len(cases) != len(request.cases):
            errors.append("duplicate case identity")
        if {c.profile_id for c in request.cases} != profiles:
            errors.append("cases do not cover exactly the expected profiles")
        for case in request.cases:
            errors.extend(_case_errors(case))
        for profile in sorted(profiles):
            profile_cases = [c for c in request.cases if c.profile_id == profile]
            if not any(c.withheld_relevant_sources for c in profile_cases):
                errors.append(f"{profile}: missing withheld-item positive control")
            if not any(not c.withheld_relevant_sources for c in profile_cases):
                errors.append(f"{profile}: missing ordinary evaluation case")
        expected = {
            (profile, case, rung, repeat)
            for (profile, case), rung, repeat in product(cases, rungs, _REPEATS)
        }
        observations: dict[tuple[str, str, str, int], ModelSelectionObservation] = {}
        for observation in request.observations:
            key = (
                observation.profile_id,
                observation.case_id,
                observation.rung,
                observation.repeat,
            )
            if key in observations:
                errors.append(f"duplicate observation: {key}")
            observations[key] = observation
            observation_case = cases.get((observation.profile_id, observation.case_id))
            if observation_case is not None:
                errors.extend(_observation_errors(observation_case, observation))
        for missing_key in sorted(expected - observations.keys()):
            errors.append(f"missing observation: {missing_key}")
        for unexpected_key in sorted(observations.keys() - expected):
            errors.append(f"unexpected observation: {unexpected_key}")
        if errors:
            return ModelSelectionEvaluationResult(
                request_id=request.request_id, valid=False, errors=tuple(sorted(errors))
            )

        rows: list[ModelSelectionEvaluationRow] = []
        for (profile, case_id), case in sorted(cases.items()):
            for repeat in _REPEATS:
                rows.append(
                    _score(
                        case,
                        ModelSelectionObservation(
                            profile_id=profile,
                            case_id=case_id,
                            rung=_BASELINE,
                            repeat=repeat,
                            selected_sources=_baseline(case),
                            prompt_tokens=0,
                            completion_tokens=0,
                        ),
                    )
                )
                for rung in sorted(rungs):
                    rows.append(
                        _score(case, observations[(profile, case_id, rung, repeat)])
                    )

        summaries: list[ModelSelectionEvaluationSummary] = []
        recommendations: list[ModelSelectionRecommendation] = []
        for profile in sorted(profiles):
            for rung, repeat in product([_BASELINE, *sorted(rungs)], _REPEATS):
                group = [
                    r
                    for r in rows
                    if r.profile_id == profile and r.rung == rung and r.repeat == repeat
                ]
                relevant = sum(r.relevant_count for r in group)
                recalled = sum(r.recalled_count for r in group)
                summaries.append(
                    ModelSelectionEvaluationSummary(
                        profile_id=profile,
                        rung=rung,
                        repeat=repeat,
                        relevant_count=relevant,
                        recalled_count=recalled,
                        recall=recalled / relevant,
                        package_tokens=sum(r.package_tokens or 0 for r in group),
                        prompt_tokens=sum(r.prompt_tokens for r in group),
                        completion_tokens=sum(r.completion_tokens for r in group),
                        parse_failures=sum(r.status == "parse_failure" for r in group),
                        unavailable=sum(r.status == "unavailable" for r in group),
                    )
                )
            baseline = {
                (r.case_id, r.repeat): r
                for r in rows
                if r.profile_id == profile and r.rung == _BASELINE
            }
            eligible = tuple(
                rung
                for rung in sorted(rungs)
                if all(
                    row.status == "ok"
                    and row.recall >= baseline[(row.case_id, row.repeat)].recall
                    for row in rows
                    if row.profile_id == profile and row.rung == rung
                )
            )
            recommendations.append(
                ModelSelectionRecommendation(
                    profile_id=profile, eligible_rungs=eligible
                )
            )
        return ModelSelectionEvaluationResult(
            request_id=request.request_id,
            valid=True,
            rows=tuple(rows),
            summaries=tuple(summaries),
            recommendations=tuple(recommendations),
        )
