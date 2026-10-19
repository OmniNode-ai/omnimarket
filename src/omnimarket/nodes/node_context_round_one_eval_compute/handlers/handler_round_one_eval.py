# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure scoring of recorded round-one runs against TE.1 labels (OMN-21141)."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from math import ceil, fsum, sqrt

from omnimarket.nodes.node_context_round_one_eval_compute.models import (
    EnumAnswerGrade,
    EnumPositiveControl,
    EnumRunMode,
    ModelAdmittedEvent,
    ModelCaseLabels,
    ModelCaseRungScore,
    ModelControlReport,
    ModelCostSummary,
    ModelInterval,
    ModelLabelledProposition,
    ModelLabelSpan,
    ModelModeSummary,
    ModelRoundOneEvalRequest,
    ModelRoundOneEvalResult,
    ModelRungReport,
    ModelRungRun,
)

__all__ = ["HandlerRoundOneEval"]


def _interval(successes: int, trials: int) -> ModelInterval:
    if trials == 0:
        return ModelInterval(successes=0, trials=0, rate=0.0, low=0.0, high=1.0)
    z = 1.959963984540054
    p = successes / trials
    denom = 1 + z * z / trials
    centre = (p + z * z / (2 * trials)) / denom
    half = z * sqrt(p * (1 - p) / trials + z * z / (4 * trials * trials)) / denom
    return ModelInterval(
        successes=successes,
        trials=trials,
        rate=round(p, 6),
        low=round(max(0.0, centre - half), 6),
        high=round(min(1.0, centre + half), 6),
    )


def _overlaps(event: ModelAdmittedEvent, span: ModelLabelSpan) -> bool:
    return (
        event.event_id in {span.event_id, *span.same_content_event_ids}
        and event.start < span.end
        and event.end > span.start
    )


def _matches(event: ModelAdmittedEvent, proposition: ModelLabelledProposition) -> bool:
    span = proposition.span
    return (
        event.event_id in {span.event_id, *span.same_content_event_ids}
        and event.start <= span.start
        and event.end >= span.end
        and event.kind == proposition.kind
    )


@dataclass(frozen=True)
class _RunScore:
    run: ModelRungRun
    matched: frozenset[str]
    successes: int
    trials: int
    hard_gate_met: bool
    violations: tuple[bool, ...]
    precision_successes: int
    negative_admissions: int


def _score_run(run: ModelRungRun, case: ModelCaseLabels) -> _RunScore:
    propositions = {p.label_id: p for p in case.propositions}
    matched = frozenset(
        p.label_id
        for p in case.propositions
        if any(_matches(e, p) for e in run.admitted)
    )
    corrected_violations = tuple(
        c.ruling in matched and c.correction not in matched
        for c in case.corrected_rulings
    )
    # Evaluate all controls against the original matches, independently of control order.
    uncredited = {
        c.ruling
        for c, violated in zip(
            case.corrected_rulings, corrected_violations, strict=True
        )
        if violated
    }
    recalled = matched - uncredited
    scored = set(propositions) - set(run.removed_label_ids)
    span_violations = tuple(
        any(
            e.kind == propositions[c.controls].kind
            and _overlaps(e, c.span)
            and not _matches(e, propositions[c.controls])
            for e in run.admitted
        )
        for c in case.span_controls
    )
    return _RunScore(
        run=run,
        matched=matched,
        successes=len(recalled & scored),
        trials=len(scored),
        hard_gate_met=all(
            p.label_id in recalled
            for p in case.propositions
            if p.hard_gate and p.label_id in scored
        ),
        violations=span_violations + corrected_violations,
        precision_successes=sum(
            any(_matches(e, p) for p in case.propositions) for e in run.admitted
        ),
        negative_admissions=sum(
            any(_overlaps(e, segment.span) for segment in case.negative_segments)
            for e in run.admitted
        ),
    )


def _recall(scores: Iterable[_RunScore]) -> ModelInterval:
    rows = tuple(scores)
    return _interval(sum(s.successes for s in rows), sum(s.trials for s in rows))


def _answers(answers: Iterable[EnumAnswerGrade]) -> ModelInterval:
    grades = tuple(answers)
    return _interval(
        grades.count(EnumAnswerGrade.MATCH),
        sum(g != EnumAnswerGrade.UNAVAILABLE for g in grades),
    )


def _cost(scores: list[_RunScore]) -> ModelCostSummary:
    calls = [call for score in scores for call in score.run.calls]
    latencies = sorted(call.latency_ms for call in calls)
    return ModelCostSummary(
        calls=len(calls),
        latency_p50_ms=latencies[ceil(0.5 * len(calls)) - 1] if calls else 0,
        latency_p95_ms=latencies[ceil(0.95 * len(calls)) - 1] if calls else 0,
        latency_max_ms=latencies[-1] if calls else 0,
        tokens_in=sum(c.tokens_in for c in calls),
        tokens_out=sum(c.tokens_out for c in calls),
        cost_usd=round(fsum(c.cost_usd for c in calls), 6),
    )


def _case_score(
    case: ModelCaseLabels, scores: list[_RunScore], reserve_tokens: int
) -> ModelCaseRungScore:
    modes = tuple(sorted({s.run.mode for s in scores}, key=lambda mode: mode.value))
    gates = {
        mode: all(s.hard_gate_met for s in scores if s.run.mode == mode)
        for mode in modes
    }
    return ModelCaseRungScore(
        case_id=case.case_id,
        runs=len(scores),
        recall=_recall(scores),
        hard_gate_met=all(s.hard_gate_met for s in scores),
        control_violations=sum(sum(s.violations) for s in scores),
        modes=modes,
        whole_chunked_disagree=len(modes) == 2
        and gates[EnumRunMode.WHOLE] != gates[EnumRunMode.CHUNKED],
        single_mode_where_both_allowed=len(modes) == 1
        and min(s.run.window_tokens for s in scores)
        >= case.transcript_tokens + reserve_tokens,
    )


def _rung_report(
    rung_id: str,
    all_scores: list[_RunScore],
    request: ModelRoundOneEvalRequest,
    native_recall: ModelInterval,
    falsifiers: set[str],
) -> ModelRungReport:
    scores = [s for s in all_scores if not s.run.removed_label_ids]
    controls = [s for s in all_scores if s.run.removed_label_ids]
    positive_control = EnumPositiveControl.ABSENT
    if controls:
        positive_control = (
            EnumPositiveControl.MISS_SCORED
            if all(
                not s.matched.intersection(s.run.removed_label_ids) for s in controls
            )
            else EnumPositiveControl.NOT_SCORED
        )
    if positive_control == EnumPositiveControl.NOT_SCORED:
        falsifiers.add(f"{rung_id}: positive control did not score the miss")

    cases = {c.case_id: c for c in request.cases}
    case_ids = sorted({s.run.case_id for s in scores})
    per_case: list[ModelCaseRungScore] = []
    reports: list[ModelControlReport] = []
    for case_id in case_ids:
        case = cases[case_id]
        rows = [s for s in scores if s.run.case_id == case_id]
        summary = _case_score(case, rows, request.reserve_tokens)
        per_case.append(summary)
        if summary.single_mode_where_both_allowed:
            falsifiers.add(
                f"{rung_id}/{case_id}: evaluated in one mode where its window allows both"
            )
        declarations = [
            (c.label_id, c.control_kind.value) for c in case.span_controls
        ] + [(c.label_id, "corrected_ruling") for c in case.corrected_rulings]
        for index, (label_id, kind) in enumerate(declarations):
            reports.append(
                ModelControlReport(
                    label_id=label_id,
                    case_id=case_id,
                    control_kind=kind,
                    runs_scored=len(rows),
                    violations=sum(s.violations[index] for s in rows),
                )
            )

    recall = _recall(scores)
    recall_not_below_native = not request.baselines or recall.rate >= native_recall.rate
    hard_gate_met = all(s.hard_gate_met for s in scores) and recall_not_below_native
    precision = _interval(
        sum(s.precision_successes for s in scores),
        sum(len(s.run.admitted) for s in scores),
    )
    next_action = _answers(s.run.answer for s in scores)
    baselines = [b for b in request.baselines if b.case_id in case_ids]
    native_answer = _answers(b.native_answer for b in baselines)
    without_events_answer = _answers(b.without_events_answer for b in baselines)
    next_action_met = (
        next_action.trials > 0
        and (native_answer.trials == 0 or next_action.rate >= native_answer.rate)
        and (
            without_events_answer.trials == 0
            or next_action.rate >= without_events_answer.rate
        )
    )
    duplicate_work_runs = sum(bool(s.run.proposed_already_done) for s in scores)
    passes = hard_gate_met and next_action_met and duplicate_work_runs == 0
    zero_metrics = tuple(
        sorted(
            name
            for name, interval in (
                ("recall", recall),
                ("precision", precision),
                ("next_action", next_action),
            )
            if interval.trials > 0 and interval.successes == 0
        )
    )
    if positive_control != EnumPositiveControl.MISS_SCORED:
        falsifiers.update(
            f"{rung_id}: zero {name} without a positive control"
            for name in zero_metrics
        )
    return ModelRungReport(
        rung_id=rung_id,
        served_model_ids=tuple(sorted({s.run.served_model_id for s in scores})),
        runs=len(scores),
        per_case=tuple(per_case),
        modes=tuple(
            ModelModeSummary(
                mode=mode,
                runs=sum(s.run.mode == mode for s in scores),
                recall=_recall(s for s in scores if s.run.mode == mode),
                hard_gate_met=all(
                    s.hard_gate_met for s in scores if s.run.mode == mode
                ),
            )
            for mode in sorted(
                {s.run.mode for s in scores}, key=lambda mode: mode.value
            )
        ),
        recall=recall,
        recall_not_below_native=recall_not_below_native,
        hard_gate_met=hard_gate_met,
        controls=tuple(sorted(reports, key=lambda c: (c.case_id, c.label_id))),
        precision=precision,
        negative_segment_admissions=sum(s.negative_admissions for s in scores),
        parse_failures=sum(s.run.parse_failures for s in scores),
        next_action=next_action,
        next_action_met=next_action_met,
        duplicate_work_runs=duplicate_work_runs,
        no_duplicate_work=duplicate_work_runs == 0,
        positive_control=positive_control,
        passes_metrics_1_to_3=passes,
        cost=_cost(scores) if passes else None,
        zero_metrics=zero_metrics,
    )


def _identity_mismatches(
    runs: tuple[ModelRungRun, ...], field: str, falsifiers: set[str]
) -> tuple[str, ...]:
    values: dict[str, set[str]] = {}
    for run in runs:
        if not run.removed_label_ids:
            values.setdefault(run.case_id, set()).add(getattr(run, field))
    mismatches = tuple(
        sorted(case_id for case_id, ids in values.items() if len(ids) > 1)
    )
    falsifiers.update(
        f"{case_id}: {field} differs between runs" for case_id in mismatches
    )
    return mismatches


def _replay(
    runs: tuple[ModelRungRun, ...], falsifiers: set[str]
) -> tuple[tuple[str, ...], int]:
    mismatches: list[str] = []
    unreplayed = 0
    for run in runs:
        pairs = (
            (
                "model_outputs_hash",
                run.model_outputs_hash,
                run.replayed_model_outputs_hash,
            ),
            ("final_pack_hash", run.final_pack_hash, run.replayed_final_pack_hash),
            ("body_hash", run.body_hash, run.replayed_body_hash),
        )
        unreplayed += all(replayed is None for _, _, replayed in pairs)
        for field, original, replayed in pairs:
            if replayed is not None and replayed != original:
                mismatch = f"{run.rung_id}/{run.case_id}/r{run.repeat}/{run.mode.value}:{field}"
                mismatches.append(mismatch)
                falsifiers.add(f"{mismatch} did not reproduce on replay")
    if unreplayed:
        falsifiers.add(f"{unreplayed} run(s) carry no replay hashes")
    return tuple(sorted(mismatches)), unreplayed


class HandlerRoundOneEval:
    """Score proposition recall, controls, answers, identity, replay and eligible costs."""

    def handle(self, request: ModelRoundOneEvalRequest) -> ModelRoundOneEvalResult:
        cases = {c.case_id: c for c in request.cases}
        native_recall = _interval(
            sum(len(b.native_recalled_label_ids) for b in request.baselines),
            sum(len(cases[b.case_id].propositions) for b in request.baselines),
        )
        by_rung: dict[str, list[_RunScore]] = {}
        for run in request.runs:
            by_rung.setdefault(run.rung_id, []).append(
                _score_run(run, cases[run.case_id])
            )
        falsifiers: set[str] = set()
        rungs = tuple(
            _rung_report(rung_id, by_rung[rung_id], request, native_recall, falsifiers)
            for rung_id in sorted(by_rung)
        )
        candidate_mismatches = _identity_mismatches(
            request.runs, "candidate_id", falsifiers
        )
        assembly_mismatches = _identity_mismatches(
            request.runs, "assembly_hash", falsifiers
        )
        replay_mismatches, unreplayed = _replay(request.runs, falsifiers)
        return ModelRoundOneEvalResult(
            rungs=rungs,
            native_recall=native_recall,
            native_next_action=_answers(b.native_answer for b in request.baselines),
            without_events_next_action=_answers(
                b.without_events_answer for b in request.baselines
            ),
            candidate_id_mismatches=candidate_mismatches,
            assembly_hash_mismatches=assembly_mismatches,
            replay_mismatches=replay_mismatches,
            unreplayed_runs=unreplayed,
            eligible_rungs=tuple(
                r.rung_id
                for r in rungs
                if r.passes_metrics_1_to_3
                and r.positive_control == EnumPositiveControl.MISS_SCORED
            ),
            falsifiers=tuple(sorted(falsifiers)),
        )
