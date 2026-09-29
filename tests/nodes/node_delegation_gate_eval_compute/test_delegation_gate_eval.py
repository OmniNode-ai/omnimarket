# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Definition-B gate calibration contract (OMN-19792).

These imports intentionally fail until the node is implemented. No live gate,
execution, or model is needed: replay verdicts are keyed by item id, except for
the explicit test of the default gate's deterministic local grader.
"""

from __future__ import annotations

import inspect
import math
from collections import Counter
from collections.abc import Callable
from typing import Literal
from unittest.mock import Mock

import pytest

from omnimarket.delegation.shadow_comparison.harness import (
    grade_like_the_local_path,
)
from omnimarket.delegation.shadow_comparison.models import ModelShadowPrompt
from omnimarket.models.ranges import EnumRangeVerdict
from omnimarket.nodes.node_delegation_gate_eval_compute.handlers import (
    handler_delegation_gate_eval as handler_module,
)
from omnimarket.nodes.node_delegation_gate_eval_compute.handlers.handler_delegation_gate_eval import (
    HandlerDelegationGateEval,
)
from omnimarket.nodes.node_delegation_gate_eval_compute.models.enum_gate_eval_label import (
    EnumGateEvalLabel,
)
from omnimarket.nodes.node_delegation_gate_eval_compute.models.enum_gate_eval_run_status import (
    EnumGateEvalRunStatus,
)
from omnimarket.nodes.node_delegation_gate_eval_compute.models.enum_gate_verdict import (
    EnumGateVerdict,
)
from omnimarket.nodes.node_delegation_gate_eval_compute.models.model_delegation_gate_eval_request import (
    ModelDelegationGateEvalRequest,
)
from omnimarket.nodes.node_delegation_gate_eval_compute.models.model_delegation_gate_eval_result import (
    ModelDelegationGateEvalResult,
)
from omnimarket.nodes.node_delegation_gate_eval_compute.models.model_gate_check_skip import (
    ModelGateCheckSkip,
)
from omnimarket.nodes.node_delegation_gate_eval_compute.models.model_gate_eval_item import (
    ModelGateEvalItem,
)
from omnimarket.nodes.node_delegation_gate_eval_compute.models.model_gate_execution_result import (
    ModelGateExecutionResult,
)
from omnimarket.nodes.node_delegation_gate_eval_compute.models.model_gate_rate_row import (
    ModelGateRateRow,
)
from omnimarket.nodes.node_delegation_gate_eval_compute.models.model_gate_replay_verdict import (
    ModelGateReplayVerdict,
)

pytestmark = pytest.mark.unit

_ARMS: tuple[Literal["recorded", "replayed"], ...] = ("recorded", "replayed")


def _item(
    item_id: str,
    *,
    label: EnumGateEvalLabel = EnumGateEvalLabel.ADEQUATE,
    task_class: str = "summarization",
    stratum: str = "default",
    recorded_verdict: EnumGateVerdict | None = EnumGateVerdict.ACCEPTED,
    requires_execution: bool = False,
    execution_result: ModelGateExecutionResult | None = None,
    checks_declared: tuple[str, ...] = (),
    recorded_checks_refused: tuple[str, ...] = (),
    recorded_checks_skipped: tuple[ModelGateCheckSkip, ...] = (),
) -> ModelGateEvalItem:
    return ModelGateEvalItem(
        item_id=item_id,
        task_class=task_class,
        stratum=stratum,
        label=label,
        prompt_text="Summarize: The release passed its checks and shipped on Monday.",
        recorded_answer="The release shipped on Monday after passing its checks.",
        recorded_verdict=recorded_verdict,
        recorded_deciding_check=None,
        requires_execution=requires_execution,
        execution_result=execution_result,
        checks_declared=checks_declared,
        recorded_checks_refused=recorded_checks_refused,
        recorded_checks_skipped=recorded_checks_skipped,
    )


def _accepted_items(n: int = 100, inadequate: int = 0) -> tuple[ModelGateEvalItem, ...]:
    return tuple(
        _item(
            f"item-{index:03d}",
            label=(
                EnumGateEvalLabel.INADEQUATE
                if index < inadequate
                else EnumGateEvalLabel.ADEQUATE
            ),
        )
        for index in range(n)
    )


def _verdict(
    verdict: EnumGateVerdict = EnumGateVerdict.ACCEPTED,
    *,
    deciding_check: str | None = None,
    checks_refused: tuple[str, ...] = (),
    checks_skipped: tuple[ModelGateCheckSkip, ...] = (),
) -> ModelGateReplayVerdict:
    return ModelGateReplayVerdict(
        verdict=verdict,
        deciding_check=deciding_check,
        checks_refused=checks_refused,
        checks_skipped=checks_skipped,
    )


def _stub_gate(
    verdicts: dict[str, ModelGateReplayVerdict],
) -> tuple[Callable[[ModelGateEvalItem], ModelGateReplayVerdict], Counter[str]]:
    calls: Counter[str] = Counter()

    def gate(item: ModelGateEvalItem) -> ModelGateReplayVerdict:
        calls[item.item_id] += 1
        return verdicts[item.item_id]

    return gate, calls


def _evaluate(
    items: tuple[ModelGateEvalItem, ...],
    verdicts: dict[str, ModelGateReplayVerdict] | None = None,
) -> ModelDelegationGateEvalResult:
    gate, calls = _stub_gate(
        verdicts
        if verdicts is not None
        else {item.item_id: _verdict() for item in items}
    )
    result = HandlerDelegationGateEval(gate=gate).handle(
        ModelDelegationGateEvalRequest(run_id="gate-eval-test", items=items)
    )
    assert calls == Counter({item.item_id: 3 for item in items})
    assert result.run_id == "gate-eval-test"
    assert result.status == EnumGateEvalRunStatus.COMPLETED
    assert result.failure_reasons == ()
    assert result.nondeterministic_items == ()
    return result


def _row(
    result: ModelDelegationGateEvalResult,
    *,
    arm: Literal["replayed", "recorded"] = "replayed",
    task_class: str = "summarization",
    stratum: str = "all",
) -> ModelGateRateRow:
    matches = [
        row
        for row in result.rate_rows
        if (row.task_class, row.stratum, row.arm) == (task_class, stratum, arm)
    ]
    assert len(matches) == 1
    return matches[0]


def test_line_met_at_four() -> None:
    result = _evaluate(_accepted_items(inadequate=4))
    row = _row(result)
    evaluation = row.false_pass_evaluation
    assert evaluation.verdict == EnumRangeVerdict.MET
    assert (evaluation.observed_n, evaluation.passes, evaluation.failures) == (
        100,
        96,
        4,
    )
    assert evaluation.incomplete == 0
    assert evaluation.floor == pytest.approx(0.90)
    assert evaluation.confidence == pytest.approx(0.95)
    assert evaluation.declared_n == 100
    assert (row.accepted_n, row.false_pass_count, row.total_n) == (100, 4, 100)
    assert (row.refused_n, row.undetermined_n, row.false_refusal_count) == (0, 0, 0)
    assert row.false_refusal_evaluation.observed_n == 0
    assert row.false_refusal_evaluation.verdict == EnumRangeVerdict.REFUSED
    assert (
        _row(result, stratum="default").false_pass_evaluation.verdict
        == EnumRangeVerdict.MET
    )


def test_line_missed_at_five() -> None:
    row = _row(_evaluate(_accepted_items(inadequate=5)))
    assert row.false_pass_evaluation.verdict == EnumRangeVerdict.MISSED
    assert row.false_pass_evaluation.observed_n == 100
    assert row.false_pass_evaluation.failures == row.false_pass_count == 5


def test_underpowered_refused() -> None:
    row = _row(_evaluate(_accepted_items(n=40)))
    assert row.false_pass_evaluation.verdict == EnumRangeVerdict.REFUSED
    assert row.false_pass_evaluation.observed_n == 40
    assert row.false_pass_evaluation.passes == 40
    assert row.false_pass_evaluation.failures == 0
    assert row.false_pass_evaluation.reasons


@pytest.mark.parametrize("drift", ["verdict", "deciding_check"])
def test_nondeterministic_gate_fails_run(drift: str) -> None:
    items = (_item("unstable-a"), _item("stable"), _item("unstable-b"))
    calls: Counter[str] = Counter()
    first = _verdict(deciding_check="syntax")
    changed = (
        _verdict(EnumGateVerdict.REFUSED, deciding_check="syntax")
        if drift == "verdict"
        else _verdict(deciding_check="execution")
    )
    sequences = {
        "unstable-a": (first, changed, first),
        "stable": (first, first, first),
        "unstable-b": (first, changed, first),
    }

    def gate(item: ModelGateEvalItem) -> ModelGateReplayVerdict:
        index = calls[item.item_id]
        calls[item.item_id] += 1
        return sequences[item.item_id][index]

    result = HandlerDelegationGateEval(gate=gate).handle(
        ModelDelegationGateEvalRequest(run_id="unstable-run", items=items)
    )
    assert calls == Counter({item.item_id: 3 for item in items})
    assert result.run_id == "unstable-run"
    assert result.status == EnumGateEvalRunStatus.FAILED
    assert set(result.nondeterministic_items) == {"unstable-a", "unstable-b"}
    assert len(result.failure_reasons) == 2
    for item_id in ("unstable-a", "unstable-b"):
        assert any(item_id in reason for reason in result.failure_reasons)
    assert result.rate_rows == ()
    assert result.check_records == ()


@pytest.mark.parametrize("missing", ["execution", "recorded_verdict"])
def test_undecidable_counts_against_line(missing: str) -> None:
    undecidable = _item(
        "undecidable",
        requires_execution=missing == "execution",
        recorded_verdict=None
        if missing == "recorded_verdict"
        else EnumGateVerdict.ACCEPTED,
    )
    result = _evaluate((*_accepted_items(inadequate=4), undecidable))
    assert result.undecidable_items == ("undecidable",)
    for arm in _ARMS:
        is_incomplete = missing == "execution" or arm == "recorded"
        for stratum in ("all", "default"):
            row = _row(result, arm=arm, stratum=stratum)
            evaluation = row.false_pass_evaluation
            assert evaluation.observed_n == 101
            assert evaluation.incomplete == int(is_incomplete)
            assert evaluation.failures == (5 if is_incomplete else 4)
            assert evaluation.verdict == (
                EnumRangeVerdict.MISSED if is_incomplete else EnumRangeVerdict.MET
            )
            assert row.accepted_n == (100 if is_incomplete else 101)
            assert row.false_pass_count == 4
            assert row.false_refusal_evaluation.observed_n == 0


def test_no_judge_call(monkeypatch: pytest.MonkeyPatch) -> None:
    assert (
        "judge" not in inspect.signature(HandlerDelegationGateEval.__init__).parameters
    )
    judge_spy = Mock(side_effect=AssertionError("a judge must never run"))
    calls: list[tuple[ModelShadowPrompt, str]] = []

    # Only two parameters: even explicitly passing judge=None violates the contract.
    def recorder(
        prompt: ModelShadowPrompt, content: str
    ) -> tuple[float | None, float | None]:
        calls.append((prompt, content))
        return grade_like_the_local_path(prompt, content)

    monkeypatch.setattr(handler_module, "grade_like_the_local_path", recorder)
    item = _item("default-gate")
    result = HandlerDelegationGateEval().handle(
        ModelDelegationGateEvalRequest(run_id="no-judge", items=(item,))
    )
    assert result.status == EnumGateEvalRunStatus.COMPLETED
    assert len(calls) == 3
    for prompt, content in calls:
        assert prompt.correlation_id == item.item_id
        assert prompt.task_type == item.task_class
        assert prompt.prompt == item.prompt_text
        assert content == item.recorded_answer
    judge_spy.assert_not_called()


def test_per_check_calibration_counts() -> None:
    timeout = ModelGateCheckSkip(check_id="execution", reason="timeout")
    no_target = ModelGateCheckSkip(check_id="execution", reason="no target")
    undeclared = ModelGateCheckSkip(check_id="undeclared", reason="not declared")
    items = (
        _item(
            "bad",
            label=EnumGateEvalLabel.INADEQUATE,
            checks_declared=("syntax", "execution"),
            recorded_checks_refused=("syntax",),
            recorded_checks_skipped=(timeout,),
        ),
        _item(
            "good",
            checks_declared=("syntax", "execution"),
            recorded_checks_refused=("syntax",),
            recorded_checks_skipped=(timeout,),
        ),
        _item(
            "unknown",
            label=EnumGateEvalLabel.UNLABELABLE,
            checks_declared=("syntax", "execution"),
            recorded_checks_refused=("syntax",),
            recorded_checks_skipped=(no_target, undeclared),
        ),
    )
    verdicts = {
        "bad": _verdict(
            EnumGateVerdict.REFUSED, checks_refused=("syntax", "execution")
        ),
        "good": _verdict(EnumGateVerdict.REFUSED, checks_refused=("execution",)),
        "unknown": _verdict(
            EnumGateVerdict.REFUSED,
            checks_refused=("syntax",),
            checks_skipped=(no_target, undeclared),
        ),
    }
    records = _evaluate(items, verdicts).check_records
    keys = [(record.check_id, record.arm) for record in records]
    assert keys == sorted(keys)
    actual = {
        (record.check_id, record.arm): (
            record.catches,
            record.wrong_refusals,
            record.skips,
            dict(record.skip_reasons),
        )
        for record in records
    }
    assert actual[("syntax", "recorded")] == (1, 1, 0, {})
    assert actual[("syntax", "replayed")] == (1, 0, 0, {})
    assert actual[("execution", "recorded")] == (
        0,
        0,
        3,
        {"timeout": 2, "no target": 1},
    )
    assert actual[("execution", "replayed")] == (1, 1, 1, {"no target": 1})
    assert len(keys) == len(actual)
    for record in records:
        assert sum(count for _, count in record.skip_reasons) == record.skips
        if record.check_id == "undeclared":
            assert (record.catches, record.wrong_refusals, record.skips) == (0, 0, 0)


def test_unlabelable_excluded_and_counted() -> None:
    unknowns = tuple(
        _item(
            f"unknown-{verdict.value}",
            label=EnumGateEvalLabel.UNLABELABLE,
            recorded_verdict=verdict,
        )
        for verdict in EnumGateVerdict
    )
    items = (*_accepted_items(inadequate=4), *unknowns)
    verdicts = {item.item_id: _verdict() for item in items}
    verdicts.update(
        {
            item.item_id: _verdict(item.recorded_verdict)
            for item in unknowns
            if item.recorded_verdict is not None
        }
    )
    result = _evaluate(items, verdicts)
    assert result.unlabelable_count == 3
    for arm in _ARMS:
        for stratum in ("all", "default"):
            row = _row(result, arm=arm, stratum=stratum)
            assert (row.total_n, row.accepted_n, row.refused_n, row.undetermined_n) == (
                100,
                100,
                0,
                0,
            )
            assert row.false_pass_evaluation.observed_n == 100
            assert row.false_pass_evaluation.verdict == EnumRangeVerdict.MET
            assert row.false_refusal_evaluation.observed_n == 0


def test_recorded_arm_uses_recorded_verdict() -> None:
    items = tuple(
        _item(
            f"item-{index:03d}",
            recorded_verdict=EnumGateVerdict.REFUSED,
            label=EnumGateEvalLabel.ADEQUATE
            if index < 3
            else EnumGateEvalLabel.INADEQUATE,
        )
        for index in range(60)
    )
    result = _evaluate(items)
    replayed, recorded = _row(result), _row(result, arm="recorded")
    assert (replayed.accepted_n, replayed.refused_n, replayed.false_pass_count) == (
        60,
        0,
        57,
    )
    assert (recorded.accepted_n, recorded.refused_n, recorded.false_refusal_count) == (
        0,
        60,
        3,
    )
    assert recorded.false_pass_evaluation.verdict == EnumRangeVerdict.REFUSED
    assert recorded.false_pass_evaluation.observed_n == 0
    evaluation = recorded.false_refusal_evaluation
    assert (evaluation.observed_n, evaluation.passes, evaluation.failures) == (
        60,
        57,
        3,
    )
    assert evaluation.floor == pytest.approx(0.80)
    assert evaluation.confidence == pytest.approx(0.95)
    assert evaluation.declared_n == 60
    assert evaluation.verdict == EnumRangeVerdict.MET
    assert replayed.false_refusal_evaluation.observed_n == 0


@pytest.mark.parametrize("arm", ["recorded", "replayed"])
@pytest.mark.parametrize("line", ["false_pass", "false_refusal"])
def test_wilson_interval_reference_values(
    arm: Literal["recorded", "replayed"], line: str
) -> None:
    verdict = (
        EnumGateVerdict.ACCEPTED if line == "false_pass" else EnumGateVerdict.REFUSED
    )
    items = tuple(
        _item(
            f"item-{index:03d}",
            recorded_verdict=verdict,
            label=(
                EnumGateEvalLabel.INADEQUATE
                if (index < 4) == (line == "false_pass")
                else EnumGateEvalLabel.ADEQUATE
            ),
        )
        for index in range(100)
    )
    row = _row(
        _evaluate(items, {item.item_id: _verdict(verdict) for item in items}), arm=arm
    )
    interval = (
        row.false_pass_wilson if line == "false_pass" else row.false_refusal_wilson
    )
    n, errors, z = 100, 4, 1.959963984540054
    p = errors / n
    denominator = 1 + z**2 / n
    center = (p + z**2 / (2 * n)) / denominator
    half_width = z * math.sqrt(p * (1 - p) / n + z**2 / (4 * n**2)) / denominator
    assert interval.low == pytest.approx(center - half_width, abs=1e-8)
    assert interval.high == pytest.approx(center + half_width, abs=1e-8)
    assert round(interval.low, 4) == 0.0157
    assert round(interval.high, 4) == 0.0984


def test_handler_is_deterministic() -> None:
    items = (
        _item(
            "z-refused",
            task_class="zeta",
            stratum="checks_only",
            recorded_verdict=EnumGateVerdict.REFUSED,
        ),
        _item(
            "a-unknown",
            task_class="alpha",
            stratum="aardvark",
            recorded_verdict=EnumGateVerdict.UNDETERMINED,
        ),
        _item(
            "a-accepted",
            task_class="alpha",
            stratum="execution",
            requires_execution=True,
            execution_result=ModelGateExecutionResult(passed=True),
        ),
    )
    gate, calls = _stub_gate(
        {
            "z-refused": _verdict(EnumGateVerdict.REFUSED),
            "a-unknown": _verdict(EnumGateVerdict.UNDETERMINED),
            "a-accepted": _verdict(),
        }
    )
    handler = HandlerDelegationGateEval(gate=gate)
    request = ModelDelegationGateEvalRequest(run_id="repeatable", items=items)
    first = handler.handle(request)
    second = handler.handle(request)
    assert first == second
    assert calls == Counter({item.item_id: 6 for item in items})
    assert first.status == EnumGateEvalRunStatus.COMPLETED
    assert first.undecidable_items == ()
    keys = [(row.task_class, row.stratum, row.arm) for row in first.rate_rows]
    assert keys == [
        (task_class, stratum, arm)
        for task_class, strata in (
            ("alpha", ("all", "aardvark", "execution")),
            ("zeta", ("all", "checks_only")),
        )
        for stratum in strata
        for arm in _ARMS
    ]
    for arm in _ARMS:
        row = _row(first, arm=arm, task_class="alpha")
        assert (row.total_n, row.accepted_n, row.undetermined_n) == (2, 1, 1)
        assert row.undetermined_share == pytest.approx(0.5)
        assert row.false_pass_evaluation.observed_n == 1
        assert row.false_refusal_evaluation.observed_n == 0
        unknown = _row(first, arm=arm, task_class="alpha", stratum="aardvark")
        assert unknown.undetermined_share == pytest.approx(1.0)
        assert unknown.false_pass_evaluation.observed_n == 0
        assert unknown.false_pass_evaluation.verdict == EnumRangeVerdict.REFUSED
        assert unknown.false_refusal_evaluation.verdict == EnumRangeVerdict.REFUSED
