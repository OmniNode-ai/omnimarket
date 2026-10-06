# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18278: strip one declared leading block and refuse residual traces."""

import json
from uuid import uuid4

import pytest
from omnibase_core.models.delegation.wire import EnumQualityRuleEnforcement

from omnimarket.delegation.reasoning_preamble import (
    RESIDUAL_REASONING_TAG_CHECK_NAME,
    UNRESOLVED_PREAMBLE_CHECK_NAME,
    EnumReasoningBoundaryRule,
    has_leading_reasoning_trace,
    segment_reasoning_preamble,
    strip_leading_inline_reasoning,
)
from omnimarket.enums.enum_provider_finish_reason import EnumProviderFinishReason
from omnimarket.inference.provider_finish_reason import TRUNCATION_CHECK_NAME
from omnimarket.inference.task_class_authority import (
    ModelReasoningPreamblePolicy,
    resolve_reasoning_preamble_policy,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate import (
    delta,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_quality_gate_input import (
    ModelQualityGateInput,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("content", "terminator", "expected", "count"),
    [
        ("reasoning</think>\n\nanswer", None, "reasoning</think>\n\nanswer", 0),
        ("reasoning</think>\n\nanswer", "</think>", "answer", 19),
        ("answer", "</think>", "answer", 0),
        ("a</think>b</think>c", "</think>", "b</think>c", 9),
        ("aEND \n answer  \n", "END", "answer  \n", 7),
    ],
)
def test_strip_only_one_leading_block(
    content: str, terminator: str | None, expected: str, count: int
) -> None:
    assert strip_leading_inline_reasoning(content, terminator) == (expected, count)


def _input(content: str) -> ModelQualityGateInput:
    return ModelQualityGateInput(
        correlation_id=uuid4(),
        task_type="document",
        llm_response_content=content,
        dod_deterministic=("response_non_empty",),
        dod_heuristic=(),
    )


@pytest.mark.parametrize(
    ("answer", "tag"),
    [
        ("Part one.</think> more reasoning. Final.", "</think>"),
        ("Final answer with <think> inside", "<think>"),
        ("Part one. <think>more reasoning</think> Final.", "<think>"),
    ],
)
@pytest.mark.parametrize("adapter_stripped", [False, True])
def test_residual_tag_refuses_and_climbs(
    answer: str, tag: str, adapter_stripped: bool
) -> None:
    raw = "We need answer user...</think>\n\n" + answer
    content, count = (
        strip_leading_inline_reasoning(raw, "</think>")
        if adapter_stripped
        else (raw, 0)
    )
    result = delta(_input(content), reasoning_stripped_chars=count)
    assert not result.passed
    assert result.fail_category == "fail_deterministic"
    assert result.quality_score == 0.0
    assert result.fallback_recommended
    assert all(reason.startswith("WEAK_OUTPUT:") for reason in result.failure_reasons)
    assert len(result.rule_evaluations) == 1
    rule = result.rule_evaluations[0]
    assert rule.rule == RESIDUAL_REASONING_TAG_CHECK_NAME
    assert rule.enforcement is EnumQualityRuleEnforcement.BLOCKING
    assert not rule.passed
    assert tag in rule.detail


def test_a_leading_paired_block_is_segmented_off_and_the_gate_refuses() -> None:
    answer = json.dumps({"answer": "42"})
    content = "  <think>weighing options</think>\n\n" + answer
    segmentation = segment_reasoning_preamble(content)
    assert segmentation.boundary_rule is EnumReasoningBoundaryRule.LEADING_PAIRED_BLOCK
    assert segmentation.answer == answer
    contract: dict[str, object] = {"type": "object", "required": ["answer"]}
    result = delta(_input(content), response_contract=contract)
    assert not result.passed
    assert result.rule_evaluations[0].rule == "no_leading_reasoning_trace"
    assert result.reasoning_preamble_rule == "leading_paired_block"


def test_a_paired_block_after_the_answer_starts_is_left_for_the_floor() -> None:
    content = "The answer is 42. <think>second thoughts</think> Or 41."
    segmentation = segment_reasoning_preamble(content)
    assert segmentation.boundary_rule is EnumReasoningBoundaryRule.NO_BOUNDARY_FOUND
    result = delta(_input(content))
    assert not result.passed
    assert result.rule_evaluations[0].rule == RESIDUAL_REASONING_TAG_CHECK_NAME


def test_a_leading_paired_block_with_nothing_behind_it_stays_unresolved() -> None:
    segmentation = segment_reasoning_preamble("<think>only reasoning</think>\n")
    assert segmentation.boundary_rule is EnumReasoningBoundaryRule.PREAMBLE_UNRESOLVED


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("<think>weighing options</think>\n\nThe answer is 42.", True),
        ("<think>only reasoning</think>\n", True),
        ("The answer is 42.", False),
        ("The answer is 42. <think>second thoughts</think> Or 41.", False),
    ],
)
def test_has_leading_reasoning_trace_names_the_raw_text_the_gate_judges(
    content: str, expected: bool
) -> None:
    assert has_leading_reasoning_trace(segment_reasoning_preamble(content)) is expected


def test_residual_opening_tag_in_whole_answer_is_refused() -> None:
    result = delta(_input("Final answer with <think> inside"))
    assert not result.passed
    assert result.rule_evaluations[0].rule == RESIDUAL_REASONING_TAG_CHECK_NAME
    assert result.reasoning_preamble_rule == "no_boundary_found"


@pytest.mark.parametrize("adapter_stripped", [False, True])
def test_clean_answer_passes_but_a_stripped_trace_still_fails(
    adapter_stripped: bool,
) -> None:
    answer = json.dumps({"answer": "Final answer."})
    raw = "We need answer user...</think>\n\n" + answer
    content, count = (
        strip_leading_inline_reasoning(raw, "</think>")
        if adapter_stripped
        else (raw, 0)
    )
    contract: dict[str, object] = {"type": "object", "required": ["answer"]}
    clean = delta(_input(answer), response_contract=contract)
    segmented = delta(
        _input(content), reasoning_stripped_chars=count, response_contract=contract
    )
    assert clean.passed
    assert not segmented.passed
    assert segmented.quality_score == 0.0
    assert segmented.rule_evaluations[0].rule == "no_leading_reasoning_trace"


def test_policy_exposes_residual_tags_and_defaults_to_none_declared() -> None:
    policy = resolve_reasoning_preamble_policy()
    assert policy is not None
    assert policy.residual_trace_tags == ("<think>", "</think>")
    without_tags = ModelReasoningPreamblePolicy.model_validate(
        policy.model_dump(exclude={"residual_trace_tags"})
    )
    assert without_tags.residual_trace_tags == ()


def test_undeclared_residual_tags_do_not_add_a_floor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = resolve_reasoning_preamble_policy()
    assert policy is not None
    monkeypatch.setattr(
        "omnimarket.nodes.node_delegation_quality_gate_reducer.handlers."
        "handler_quality_gate.resolve_reasoning_preamble_policy",
        lambda: policy.model_copy(update={"residual_trace_tags": ()}),
    )
    assert delta(
        _input(json.dumps({"answer": "Final answer with <think> inside"})),
        response_contract={"type": "object", "required": ["answer"]},
    ).passed


@pytest.mark.parametrize(
    ("content", "finish_reason", "expected_rule"),
    [
        (
            "Final answer with <think> inside",
            EnumProviderFinishReason.LENGTH,
            TRUNCATION_CHECK_NAME,
        ),
        (
            "We need answer user... <think> unfinished",
            EnumProviderFinishReason.STOP,
            UNRESOLVED_PREAMBLE_CHECK_NAME,
        ),
    ],
)
def test_existing_floors_keep_precedence(
    content: str, finish_reason: EnumProviderFinishReason, expected_rule: str
) -> None:
    result = delta(_input(content), finish_reason=finish_reason)
    assert result.rule_evaluations[0].rule == expected_rule


@pytest.mark.parametrize("task_type", ["document", "code_generation", "unknown-task"])
@pytest.mark.parametrize("with_contract", [False, True])
@pytest.mark.parametrize("adapter_stripped", [False, True])
@pytest.mark.parametrize(
    "trace",
    [
        "We need answer user...</think>\n\n",
        "<think>weighing options</think>\n\n",
        "Here's a thinking process:\n\n# Answer\n",
    ],
)
def test_leading_trace_is_a_blocking_floor_even_with_a_complete_answer(
    task_type: str,
    with_contract: bool,
    adapter_stripped: bool,
    trace: str,
) -> None:
    answer = json.dumps({"answer": "Final answer."})
    contract: dict[str, object] | None = (
        {"type": "object", "required": ["answer"]} if with_contract else None
    )
    result = delta(
        _input(answer if adapter_stripped else trace + answer).model_copy(
            update={"task_type": task_type}
        ),
        reasoning_stripped_chars=len(trace) if adapter_stripped else 0,
        response_contract=contract,
        judge_adequacy_score=1.0,
    )
    assert not result.passed
    assert result.fail_category == "fail_deterministic"
    assert result.quality_score == 0.0
    assert result.fallback_recommended
    rule = result.rule_evaluations[0]
    assert rule.rule == "no_leading_reasoning_trace"
    assert rule.enforcement is EnumQualityRuleEnforcement.BLOCKING
    assert not rule.passed
    assert "leading reasoning trace" in rule.detail
    clean = delta(
        _input(answer).model_copy(update={"task_type": task_type}),
        response_contract=contract,
        judge_adequacy_score=1.0,
    )
    if with_contract:
        assert clean.passed
