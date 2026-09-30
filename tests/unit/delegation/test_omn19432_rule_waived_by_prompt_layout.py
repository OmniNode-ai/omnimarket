# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19432: a rule the prompt's own layout contradicts is neither asked for nor enforced.

Measured 2026-09-30 (GLM lineup audit correction, knowledge-base-internal#905):
nine of the sixty "wrong" glm-5.3 answers were the model obeying the delegate's
injected ``step_by_step_explanation`` directive ("Show how the answer is reached as
numbered steps (1., 2., ...), then state the conclusion.") on a reasoning task whose
own prompt said "one line per item", "under 90 words, plain prose, no list" or "output
only the lines". The judge never saw the directive and scored the model wrong, and the
gate holds the same rule as a BLOCKING veto, so the reverse also failed: an answer that
obeyed the task's own layout was refused for having no numbered steps. Four more were a
`review` task whose prompt said "Answer with only one JSON object", refused by
``cites_specific_lines`` ("missing specific line citations") on a JSON classification.

The fix is a declared per-rule waiver, not a new shape: a quality rule may declare
``waived_when_prompt_matches``, prompt patterns for layouts that contradict it. The
waiver is resolved with the DoD bands, so what the model is told and what the gate
enforces are the same list (the OMN-18349 property), and it lowers only the HEURISTIC
band, never the deterministic floor.
"""

from __future__ import annotations

import pytest

from omnimarket.delegation.acceptance_directives import (
    acceptance_rule_names,
    render_acceptance_directives,
)
from omnimarket.inference.task_class_authority import resolve_quality_rule
from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
    resolve_task_class_dod_checks,
    resolve_task_class_dod_resolution,
)

pytestmark = pytest.mark.unit

_ONE_LINE_PER_ITEM = (
    "Classify each red check into exactly one class from: real_defect, stale_base, "
    "aggregate_follower. One line per check: name, class, one short reason. Use only "
    "these facts. Facts: 1. Build: log shows a compile error. 2. CI Summary: an "
    "aggregate of the above."
)
_UNDER_N_WORDS_NO_LIST = (
    "In under 90 words, plain prose, no list, classify this CI failure: its unit shard "
    "failed once on a flaky fixture and a scheduled job has failed four runs in a row. "
    "Say which class each one is and the one action each needs."
)
_OUTPUT_ONLY_LINES = (
    "Reply as one line per item: item | class | one-sentence reason. Facts only from "
    "this table. 1. job A red, cancelled by a newer run. 2. job B red, inherited from base."
)
_JSON_ONLY = (
    'Classify this leftover git worktree. Facts: {"dirty":0,"unpushed_commits":2}. '
    'Answer only JSON {"decision":"keep" or "pin_and_remove","rationale":"one sentence"}.'
)
_ONE_WORD_THEN_SENTENCE = (
    "Classify this CI red as REAL or CASCADE. Answer one word then one sentence. Log: "
    "the shard did not run because the upstream job was skipped."
)
_OPEN_QUESTION = (
    "Explain why a retry storm builds up when a consumer commits offsets late, and "
    "compare the two mitigations we have discussed."
)
_CODE_REVIEW = (
    "Review this diff for bugs. Reply in 5 lines max. diff --git a/x.py b/x.py\n"
    "@@ -1,3 +1,4 @@\n+    return str(value)\n"
)


def _heuristic(task_type: str, prompt: str) -> tuple[str, ...]:
    return resolve_task_class_dod_checks(task_type, prompt=prompt)[1]


@pytest.mark.parametrize(
    "prompt",
    [
        _ONE_LINE_PER_ITEM,
        _UNDER_N_WORDS_NO_LIST,
        _OUTPUT_ONLY_LINES,
        _ONE_WORD_THEN_SENTENCE,
    ],
)
@pytest.mark.parametrize("task_type", ["reasoning", "complex_reasoning"])
def test_numbered_steps_rule_is_waived_when_the_prompt_sets_its_own_layout(
    task_type: str, prompt: str
) -> None:
    assert "step_by_step_explanation" not in _heuristic(task_type, prompt)


@pytest.mark.parametrize("task_type", ["reasoning", "complex_reasoning"])
def test_numbered_steps_rule_still_applies_to_an_open_question(task_type: str) -> None:
    """Positive control: the waiver is not a deletion of the rule."""
    assert "step_by_step_explanation" in _heuristic(task_type, _OPEN_QUESTION)


def test_the_waiver_removes_the_directive_the_model_is_told() -> None:
    """The model must not be asked for numbered steps the gate no longer wants."""
    deterministic, heuristic = resolve_task_class_dod_checks(
        "reasoning", prompt=_ONE_LINE_PER_ITEM
    )
    rendered = render_acceptance_directives(
        acceptance_rule_names(
            dod_deterministic=deterministic,
            dod_heuristic=heuristic,
            acceptance_criteria=(),
            quality_contract_mode="extend_task_class",
        )
    )
    assert "numbered steps" not in (rendered or "")


def test_the_directive_is_still_told_for_an_open_question() -> None:
    deterministic, heuristic = resolve_task_class_dod_checks(
        "reasoning", prompt=_OPEN_QUESTION
    )
    rendered = render_acceptance_directives(
        acceptance_rule_names(
            dod_deterministic=deterministic,
            dod_heuristic=heuristic,
            acceptance_criteria=(),
            quality_contract_mode="extend_task_class",
        )
    )
    assert "numbered steps" in (rendered or "")


def test_a_json_only_classification_is_not_required_to_cite_code_lines() -> None:
    assert "cites_specific_lines" not in _heuristic("review", _JSON_ONLY)


def test_a_layout_waiver_does_not_lower_a_code_review() -> None:
    """`Reply in 5 lines max` bounds length; a code review still cites the line."""
    assert "cites_specific_lines" in _heuristic("code_review", _CODE_REVIEW)


def test_a_waiver_never_lowers_the_deterministic_floor() -> None:
    plain = resolve_task_class_dod_checks("reasoning", prompt=_OPEN_QUESTION)[0]
    waived = resolve_task_class_dod_checks("reasoning", prompt=_ONE_LINE_PER_ITEM)[0]
    assert waived == plain
    assert "response_non_empty" in waived


def test_the_adequacy_authority_and_grounding_survive_a_waiver() -> None:
    """Only the contradicted rule goes: the semantic and grounding vetoes stay."""
    heuristic = _heuristic("reasoning", _ONE_LINE_PER_ITEM)
    assert "semantic_adequacy" in heuristic
    assert "identifiers_grounded" in heuristic
    assert "no_refusal" in heuristic


def test_the_resolution_records_which_rules_the_prompt_waived() -> None:
    waived = resolve_task_class_dod_resolution("reasoning", _ONE_LINE_PER_ITEM)
    assert waived.waived_rules == ("step_by_step_explanation",)
    open_question = resolve_task_class_dod_resolution("reasoning", _OPEN_QUESTION)
    assert open_question.waived_rules == ()


def test_no_prompt_resolves_the_class_band_unchanged() -> None:
    """``prompt=None`` callers keep the exact pre-change band (no waiver can fire)."""
    _, heuristic = resolve_task_class_dod_checks("reasoning")
    assert "step_by_step_explanation" in heuristic


@pytest.mark.parametrize("rule", ["step_by_step_explanation", "cites_specific_lines"])
def test_the_waiver_is_declared_in_the_rule_not_in_python(rule: str) -> None:
    declared = resolve_quality_rule(rule)
    assert declared is not None
    assert declared.waived_when_prompt_matches, (
        f"{rule} must declare waived_when_prompt_matches in task_class_contracts.v1.yaml"
    )
