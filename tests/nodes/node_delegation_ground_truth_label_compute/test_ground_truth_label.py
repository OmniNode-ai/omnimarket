# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19787: label delegated runs from evidence the gate never produced."""

from __future__ import annotations

from importlib import import_module

import pytest

from omnimarket.adapters.codex.local_runtime_dispatch import _resolve_node_route
from omnimarket.events.delegation_gate_eval.enum_gate_eval_label import (
    EnumGateEvalLabel,
)
from omnimarket.events.delegation_gate_eval.enum_gate_verdict import EnumGateVerdict
from omnimarket.events.delegation_gate_eval.model_delegation_gate_eval_request import (
    ModelDelegationGateEvalRequest,
)
from omnimarket.models.delegation_gate_eval.model_checklist_item_score import (
    ModelChecklistItemScore,
)
from omnimarket.models.delegation_gate_eval.model_checklist_score import (
    ModelChecklistScore,
)
from omnimarket.models.delegation_gate_eval.model_ground_truth_label_request import (
    ModelGroundTruthLabelRequest,
)
from omnimarket.models.delegation_gate_eval.model_ground_truth_run import (
    ModelGroundTruthRun,
)
from omnimarket.models.delegation_gate_eval.model_hidden_test_outcome import (
    ModelHiddenTestOutcome,
)
from omnimarket.nodes.node_delegation_gate_eval_compute.handlers.handler_delegation_gate_eval import (
    HandlerDelegationGateEval,
)
from omnimarket.nodes.node_delegation_ground_truth_label_compute.handlers.handler_ground_truth_label import (
    HandlerGroundTruthLabel,
)

pytestmark = pytest.mark.unit

HIDDEN = (
    "from solution import parse_duration\n\n"
    "def test_order_enforced():\n"
    "    assert_raises_value_error(lambda: parse_duration('1m1h'))\n"
)
PROMPT = "Write parse_duration(s) returning seconds; units h, m, s in that order."


def _code_run(
    run_id: str,
    *,
    passed: int,
    total: int = 4,
    extracted: bool = True,
    verdict: EnumGateVerdict = EnumGateVerdict.ACCEPTED,
    prompt: str = PROMPT,
) -> ModelGroundTruthRun:
    return ModelGroundTruthRun(
        run_id=run_id,
        task_class="code_generation",
        stratum="hidden_test",
        delegated_model="Qwen3.8-27B",
        prompt_text=prompt,
        answer_text="def parse_duration(s): ...",
        gate_verdict=verdict,
        hidden_test=ModelHiddenTestOutcome(
            test_id="parse_duration",
            hidden_test_source=HIDDEN,
            artifact_extracted=extracted,
            cases_total=total,
            cases_passed=passed,
        ),
    )


def _doc_run(
    run_id: str,
    *,
    met: tuple[bool | None, ...],
    scorer: str = "claude-sonnet-5-5",
    verdict: EnumGateVerdict = EnumGateVerdict.ACCEPTED,
) -> ModelGroundTruthRun:
    return ModelGroundTruthRun(
        run_id=run_id,
        task_class="document",
        stratum="checklist",
        delegated_model="Qwen3.8-27B",
        prompt_text="Document the --since flag from these facts.",
        answer_text="The --since flag ...",
        gate_verdict=verdict,
        checklist=ModelChecklistScore(
            checklist_id="since_flag",
            scorer_model=scorer,
            items=tuple(
                ModelChecklistItemScore(item_id=f"c{index}", met=value)
                for index, value in enumerate(met)
            ),
        ),
    )


def _label(run: ModelGroundTruthRun) -> tuple[EnumGateEvalLabel, str]:
    result = HandlerGroundTruthLabel().handle(
        ModelGroundTruthLabelRequest(batch_id="b", runs=(run,))
    )
    (label,) = result.labels
    return label.label, label.reason


def test_hidden_test_all_cases_pass_is_adequate() -> None:
    assert _label(_code_run("r1", passed=4))[0] == EnumGateEvalLabel.ADEQUATE


def test_hidden_test_one_case_failing_is_inadequate() -> None:
    label, reason = _label(_code_run("r1", passed=3))
    assert label == EnumGateEvalLabel.INADEQUATE
    assert "3/4" in reason


def test_no_extractable_code_is_inadequate() -> None:
    label, reason = _label(_code_run("r1", passed=0, total=0, extracted=False))
    assert label == EnumGateEvalLabel.INADEQUATE
    assert reason == "no_extractable_artifact"


def test_hidden_test_that_ran_no_case_is_unlabelable() -> None:
    label, reason = _label(_code_run("r1", passed=0, total=0))
    assert label == EnumGateEvalLabel.UNLABELABLE
    assert reason == "hidden_test_ran_no_cases"


def test_hidden_test_seen_in_the_prompt_is_unlabelable() -> None:
    leaked = PROMPT + "\n    assert_raises_value_error(lambda: parse_duration('1m1h'))"
    label, reason = _label(_code_run("r1", passed=4, prompt=leaked))
    assert label == EnumGateEvalLabel.UNLABELABLE
    assert reason == "hidden_test_leaked_into_prompt"


def test_checklist_all_met_is_adequate() -> None:
    assert _label(_doc_run("d1", met=(True, True, True)))[0] == (
        EnumGateEvalLabel.ADEQUATE
    )


def test_checklist_unmet_item_is_inadequate_and_named() -> None:
    label, reason = _label(_doc_run("d1", met=(True, False, True)))
    assert label == EnumGateEvalLabel.INADEQUATE
    assert "c1" in reason


def test_checklist_unscored_item_is_unlabelable() -> None:
    label, reason = _label(_doc_run("d1", met=(True, None, True)))
    assert label == EnumGateEvalLabel.UNLABELABLE
    assert reason == "checklist_item_unscored:c1"


def test_checklist_scored_by_the_delegated_model_is_unlabelable() -> None:
    label, reason = _label(_doc_run("d1", met=(True, True), scorer="qwen3.8-27b"))
    assert label == EnumGateEvalLabel.UNLABELABLE
    assert reason == "scorer_not_independent"


def test_rater_role_names_the_evidence_not_the_gate() -> None:
    result = HandlerGroundTruthLabel().handle(
        ModelGroundTruthLabelRequest(
            batch_id="b",
            runs=(_code_run("r1", passed=4), _doc_run("d1", met=(True,))),
        )
    )
    assert result.batch_id == "b"
    assert [label.rater_role for label in result.labels] == [
        "hidden_test:parse_duration",
        "checklist:claude-sonnet-5-5:since_flag",
    ]


def test_evidence_kinds_are_exclusive() -> None:
    run = _code_run("r1", passed=4)
    with pytest.raises(ValueError, match="exactly one"):
        ModelGroundTruthRun.model_validate(
            {
                **run.model_dump(),
                "checklist": _doc_run("d", met=(True,)).checklist,
            }
        )


def test_items_feed_the_gate_eval_and_count_false_passes() -> None:
    runs = (
        _code_run("c-pass-good", passed=4),
        _code_run("c-pass-bad", passed=1),
        _code_run("c-pass-nocode", passed=0, total=0, extracted=False),
        _code_run("c-refused-good", passed=4, verdict=EnumGateVerdict.REFUSED),
        _doc_run("d-pass-good", met=(True, True)),
        _doc_run("d-pass-bad", met=(True, False)),
        _doc_run("d-refused-bad", met=(False,), verdict=EnumGateVerdict.REFUSED),
    )
    labelled = HandlerGroundTruthLabel().handle(
        ModelGroundTruthLabelRequest(batch_id="b", runs=runs)
    )
    assert [item.recorded_verdict for item in labelled.items] == [
        run.gate_verdict for run in runs
    ]
    evaluated = HandlerDelegationGateEval().handle(
        ModelDelegationGateEvalRequest(run_id="b", items=labelled.items)
    )
    rows = {
        row.task_class: row
        for row in evaluated.rate_rows
        if row.arm == "recorded" and row.stratum == "all"
    }
    assert (
        rows["code_generation"].accepted_n,
        rows["code_generation"].false_pass_count,
    ) == (3, 2)
    assert rows["code_generation"].false_refusal_count == 1
    assert (rows["document"].accepted_n, rows["document"].false_pass_count) == (2, 1)
    assert rows["document"].false_refusal_count == 0


def test_registered_route_resolves_to_the_handler() -> None:
    route = _resolve_node_route("node_delegation_ground_truth_label_compute")
    assert route.command_topic == (
        "onex.cmd.omnimarket.delegation-ground-truth-label-requested.v1"
    )
    assert route.terminal_topic == (
        "onex.evt.omnimarket.delegation-ground-truth-labelled.v1"
    )
    assert (
        getattr(import_module(route.handler_module), route.handler_class)
        is HandlerGroundTruthLabel
    )
    assert (
        getattr(import_module(route.input_model_module), route.input_model_name)
        is ModelGroundTruthLabelRequest
    )
