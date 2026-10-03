# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure definition-B gate replay and calibration (OMN-19792)."""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Callable, Sequence
from typing import Literal

from omnimarket.delegation.shadow_comparison.grader import grade_like_the_local_path
from omnimarket.delegation.shadow_comparison.models import ModelShadowPrompt
from omnimarket.events.delegation_gate_eval.enum_gate_eval_label import (
    EnumGateEvalLabel,
)
from omnimarket.events.delegation_gate_eval.enum_gate_eval_run_status import (
    EnumGateEvalRunStatus,
)
from omnimarket.events.delegation_gate_eval.enum_gate_verdict import (
    EnumGateVerdict,
)
from omnimarket.events.delegation_gate_eval.model_delegation_gate_eval_request import (
    ModelDelegationGateEvalRequest,
)
from omnimarket.events.delegation_gate_eval.model_delegation_gate_eval_result import (
    ModelDelegationGateEvalResult,
)
from omnimarket.events.delegation_gate_eval.model_gate_check_record import (
    ModelGateCheckRecord,
)
from omnimarket.events.delegation_gate_eval.model_gate_eval_item import (
    ModelGateEvalItem,
)
from omnimarket.events.delegation_gate_eval.model_gate_item_verdict import (
    ModelGateItemVerdict,
)
from omnimarket.events.delegation_gate_eval.model_gate_rate_row import (
    ModelGateRateRow,
)
from omnimarket.events.delegation_gate_eval.model_gate_replay_verdict import (
    ModelGateReplayVerdict,
)
from omnimarket.events.delegation_gate_eval.model_wilson_interval import (
    ModelWilsonInterval,
)
from omnimarket.models.ranges import (
    EnumIncompleteRunTreatment,
    EnumRangeSampleOutcome,
    ModelRangeAcceptanceLine,
    ModelRangeEvaluation,
    ModelRangeMethod,
    ModelRangeRun,
    ModelRangeSample,
)
from omnimarket.ranges import evaluate_range_line

GateReplay = Callable[[ModelGateEvalItem], ModelGateReplayVerdict]
_Arm = Literal["recorded", "replayed", "rubric"]
_ARMS: tuple[_Arm, ...] = ("recorded", "replayed")
_Replay = tuple[ModelGateEvalItem, ModelGateReplayVerdict]
_REPLAYS = 3


def _default_gate(item: ModelGateEvalItem) -> ModelGateReplayVerdict:
    if item.execution_result is not None and not item.execution_result.passed:
        return ModelGateReplayVerdict(
            verdict=EnumGateVerdict.REFUSED,
            deciding_check="execution",
            checks_refused=("execution",),
        )
    if item.recorded_answer is None:
        return ModelGateReplayVerdict(verdict=EnumGateVerdict.UNDETERMINED)
    prompt = ModelShadowPrompt(
        correlation_id=item.item_id,
        task_type=item.task_class,
        prompt=item.prompt_text,
    )
    score, bar = grade_like_the_local_path(prompt, item.recorded_answer)
    if score is None or bar is None:
        return ModelGateReplayVerdict(verdict=EnumGateVerdict.UNDETERMINED)
    return ModelGateReplayVerdict(
        verdict=EnumGateVerdict.ACCEPTED if score >= bar else EnumGateVerdict.REFUSED
    )


def wilson_interval(errors: int, n: int) -> ModelWilsonInterval:
    """Wilson 95% error interval; no observations leave the full [0, 1] range."""
    if not 0 <= errors <= n:
        raise ValueError("Wilson counts must satisfy 0 <= errors <= n")
    if n == 0:
        return ModelWilsonInterval(low=0.0, high=1.0)
    z = 1.959963984540054
    p = errors / n
    denominator = 1 + z**2 / n
    center = (p + z**2 / (2 * n)) / denominator
    half_width = z * math.sqrt(p * (1 - p) / n + z**2 / (4 * n**2)) / denominator
    return ModelWilsonInterval(
        low=max(0.0, center - half_width), high=min(1.0, center + half_width)
    )


def _rubric_gate_verdict(item: ModelGateEvalItem) -> EnumGateVerdict:
    if item.rubric_verdict is None or item.rubric_verdict.outcome == "UNDETERMINED":
        return EnumGateVerdict.UNDETERMINED
    return (
        EnumGateVerdict.ACCEPTED
        if item.rubric_verdict.outcome == "PASS"
        else EnumGateVerdict.REFUSED
    )


def _undecidable(item: ModelGateEvalItem, arm: _Arm) -> bool:
    if arm == "rubric":
        return item.rubric_verdict is None
    return (item.requires_execution and item.execution_result is None) or (
        arm == "recorded" and item.recorded_verdict is None
    )


def _evaluate_line(
    run_id: str,
    task_class: str,
    stratum: str,
    arm: _Arm,
    line_name: Literal["false_pass", "false_refusal"],
    samples: list[ModelRangeSample],
    declared_line: ModelRangeAcceptanceLine | None = None,
) -> ModelRangeEvaluation:
    false_pass = line_name == "false_pass"
    line = declared_line or ModelRangeAcceptanceLine(
        check_id=f"{task_class}:{stratum}:{arm}:{line_name}",
        statistic="pass_rate",
        case_set=f"{task_class} / {stratum} / {arm} / {line_name}",
        window=f"{task_class}: trailing 30 days",
        floor=0.90 if false_pass else 0.80,
        method=ModelRangeMethod(
            sample_size=100 if false_pass else 60,
            confidence=0.95,
            margin=0.07 if false_pass else 0.12,
            power=0.8,
            incomplete_run_treatment=EnumIncompleteRunTreatment.COUNT_AS_FAILURE,
        ),
    )
    run = ModelRangeRun(
        run_id=run_id,
        samples=tuple(samples),
        sampling_seed=None,
        temperature_forced=False,
        retried_until_pass=False,
    )
    return evaluate_range_line(line, (run,))


def _rate_row(
    run_id: str,
    task_class: str,
    stratum: str,
    arm: _Arm,
    replays: Sequence[_Replay],
    false_pass_line: ModelRangeAcceptanceLine | None = None,
    false_refusal_line: ModelRangeAcceptanceLine | None = None,
) -> ModelGateRateRow:
    false_pass_samples: list[ModelRangeSample] = []
    false_refusal_samples: list[ModelRangeSample] = []
    accepted = refused = undetermined = total = incomplete = 0
    false_pass_count = false_refusal_count = 0
    for item, replay in replays:
        if item.label == EnumGateEvalLabel.UNLABELABLE:
            continue
        total += 1
        if _undecidable(item, arm):
            incomplete += 1
            false_pass_samples.append(
                ModelRangeSample(
                    case_id=item.item_id, outcome=EnumRangeSampleOutcome.INCOMPLETE
                )
            )
            continue
        verdict = (
            _rubric_gate_verdict(item)
            if arm == "rubric"
            else replay.verdict
            if arm == "replayed"
            else item.recorded_verdict
        )
        adequate = item.label == EnumGateEvalLabel.ADEQUATE
        if verdict == EnumGateVerdict.ACCEPTED:
            accepted += 1
            false_pass_count += int(not adequate)
            false_pass_samples.append(
                ModelRangeSample(
                    case_id=item.item_id,
                    outcome=EnumRangeSampleOutcome.PASS
                    if adequate
                    else EnumRangeSampleOutcome.FAIL,
                )
            )
        elif verdict == EnumGateVerdict.REFUSED:
            refused += 1
            false_refusal_count += int(adequate)
            false_refusal_samples.append(
                ModelRangeSample(
                    case_id=item.item_id,
                    outcome=EnumRangeSampleOutcome.FAIL
                    if adequate
                    else EnumRangeSampleOutcome.PASS,
                )
            )
        else:
            undetermined += 1
            if arm == "rubric":
                false_pass_samples.append(
                    ModelRangeSample(
                        case_id=item.item_id, outcome=EnumRangeSampleOutcome.INCOMPLETE
                    )
                )
    decidable_n = total - incomplete
    return ModelGateRateRow(
        task_class=task_class,
        stratum=stratum,
        arm=arm,
        accepted_n=accepted,
        false_pass_count=false_pass_count,
        false_refusal_count=false_refusal_count,
        refused_n=refused,
        undetermined_n=undetermined,
        total_n=total,
        undetermined_share=undetermined / decidable_n if decidable_n else 0.0,
        # The interval follows the line's population and incomplete-as-failure rule.
        false_pass_wilson=wilson_interval(
            false_pass_count + incomplete + (undetermined if arm == "rubric" else 0),
            len(false_pass_samples),
        ),
        false_refusal_wilson=wilson_interval(false_refusal_count, refused),
        false_pass_evaluation=_evaluate_line(
            run_id,
            task_class,
            stratum,
            arm,
            "false_pass",
            false_pass_samples,
            false_pass_line,
        ),
        false_refusal_evaluation=_evaluate_line(
            run_id,
            task_class,
            stratum,
            arm,
            "false_refusal",
            false_refusal_samples,
            false_refusal_line,
        ),
    )


def _check_records(replays: Sequence[_Replay]) -> tuple[ModelGateCheckRecord, ...]:
    counts: dict[tuple[str, _Arm], Counter[str]] = defaultdict(Counter)
    reasons: dict[tuple[str, _Arm], Counter[str]] = defaultdict(Counter)
    for item, replay in replays:
        for arm in _ARMS:
            refused = (
                replay.checks_refused
                if arm == "replayed"
                else item.recorded_checks_refused
            )
            skipped = (
                replay.checks_skipped
                if arm == "replayed"
                else item.recorded_checks_skipped
            )
            for check_id in set(item.checks_declared) | set(refused):
                counts[(check_id, arm)]
            for check_id in set(refused):
                if item.label == EnumGateEvalLabel.INADEQUATE:
                    counts[(check_id, arm)]["catches"] += 1
                elif item.label == EnumGateEvalLabel.ADEQUATE:
                    counts[(check_id, arm)]["wrong_refusals"] += 1
            for skip in set(skipped):
                if skip.check_id in item.checks_declared:
                    key = (skip.check_id, arm)
                    counts[key]["skips"] += 1
                    reasons[key][skip.reason] += 1
    for item, _ in replays:
        verdict = item.rubric_verdict
        if verdict is None:
            continue
        for criterion in verdict.failed_criteria:
            key = (criterion, "rubric")
            counts[key]
            if item.label == EnumGateEvalLabel.INADEQUATE:
                counts[key]["catches"] += 1
            elif item.label == EnumGateEvalLabel.ADEQUATE:
                counts[key]["wrong_refusals"] += 1
        for criterion in verdict.undetermined_criteria:
            key = (criterion, "rubric")
            counts[key]["skips"] += 1
            reasons[key]["rubric_undetermined"] += 1
    return tuple(
        ModelGateCheckRecord(
            check_id=check_id,
            arm=arm,
            catches=count["catches"],
            wrong_refusals=count["wrong_refusals"],
            skips=count["skips"],
            skip_reasons=tuple(sorted(reasons[(check_id, arm)].items())),
        )
        for (check_id, arm), count in sorted(counts.items())
    )


class HandlerDelegationGateEval:
    """Replay each gate three times; calibrate supplied class rubric evidence."""

    def __init__(self, gate: GateReplay | None = None) -> None:
        self._gate = _default_gate if gate is None else gate

    def handle(
        self, request: ModelDelegationGateEvalRequest
    ) -> ModelDelegationGateEvalResult:
        replays: list[_Replay] = []
        nondeterministic: list[str] = []
        for item in request.items:
            first, second, third = (self._gate(item) for _ in range(_REPLAYS))
            decisions = {
                (verdict.verdict, verdict.deciding_check)
                for verdict in (first, second, third)
            }
            if len(decisions) != 1:
                nondeterministic.append(item.item_id)
            replays.append((item, first))
        unlabelable_count = sum(
            item.label == EnumGateEvalLabel.UNLABELABLE for item in request.items
        )
        undecidable_items = tuple(
            sorted(
                {
                    item.item_id
                    for item in request.items
                    if _undecidable(item, "recorded")
                }
            )
        )
        if nondeterministic:
            return ModelDelegationGateEvalResult(
                run_id=request.run_id,
                status=EnumGateEvalRunStatus.FAILED,
                failure_reasons=tuple(
                    f"Nondeterministic gate replay for item {item_id}"
                    for item_id in sorted(nondeterministic)
                ),
                nondeterministic_items=tuple(sorted(nondeterministic)),
                undecidable_items=undecidable_items,
                unlabelable_count=unlabelable_count,
            )
        groups: dict[tuple[str, str], list[_Replay]] = defaultdict(list)
        for item, replay in replays:
            # A literal 'all' stratum is already its class rollup: never double count.
            for stratum in {"all", item.stratum}:
                groups[(item.task_class, stratum)].append((item, replay))
        rows = tuple(
            _rate_row(
                request.run_id,
                task_class,
                stratum,
                arm,
                groups[(task_class, stratum)],
                request.rubric_false_pass_lines.get(task_class)
                if arm == "rubric"
                else None,
                request.rubric_false_refusal_lines.get(task_class)
                if arm == "rubric"
                else None,
            )
            for task_class, stratum in sorted(
                groups, key=lambda key: (key[0], key[1] != "all", key[1])
            )
            for arm in (
                (*_ARMS, "rubric")
                if task_class in request.rubric_false_pass_lines
                else _ARMS
            )
        )
        return ModelDelegationGateEvalResult(
            run_id=request.run_id,
            status=EnumGateEvalRunStatus.COMPLETED,
            undecidable_items=undecidable_items,
            unlabelable_count=unlabelable_count,
            rate_rows=rows,
            check_records=_check_records(replays),
            item_verdicts=tuple(
                ModelGateItemVerdict(
                    item_id=item.item_id,
                    task_class=item.task_class,
                    stratum=item.stratum,
                    label=item.label,
                    recorded_verdict=item.recorded_verdict,
                    recorded_deciding_check=item.recorded_deciding_check,
                    replayed=replay,
                    replay_count=_REPLAYS,
                    rubric_verdict=item.rubric_verdict,
                )
                for item, replay in sorted(replays, key=lambda pair: pair[0].item_id)
            ),
        )


__all__ = ["GateReplay", "HandlerDelegationGateEval", "wilson_interval"]
