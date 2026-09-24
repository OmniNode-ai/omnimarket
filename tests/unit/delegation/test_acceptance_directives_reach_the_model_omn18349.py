# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A blocking acceptance rule is stated to the model it binds (OMN-18349).

Measured on the lab, 2026-09-23, D11 nightly dispatched against the .201 dev
lane (run 35920604853) and replayed by hand against the same endpoint
(vLLM, Qwen3.8-27B, thinking off):

* I1 (``document``): the local model answered correctly three times and was
  refused three times. Its answer carried no ``### ANSWER`` line; the only
  place that marker is requested is the SYSTEM prompt, which Qwen3.8-27B
  obeyed on 2 of 8 document requests. The extractor then blanked the answer,
  the gate saw an empty response (score 0.267), and the run climbed to a
  metered tier. The marker sentence restated at the start of the USER turn
  was obeyed 8 of 8 times on the same prompt.
* I5 (``research``): the class blocks on ``cites_sources`` and
  ``methodical_analysis``. Nothing the model receives asks for either, and
  the class's own system prompt still asked for code-line citations, the rule
  OMN-13354 took away from research. Every local answer, and the metered
  glm-5.3-flash answer, failed ``cites_sources``.
* I6 (``code_review``): the class blocks on ``cites_specific_lines``; nothing
  asks for a line citation. Three local answers failed on it alone.

Stated in the user turn as a list of requirements, the same prompts passed the
gate's own checks 8 of 8 (research) and 8 of 8 (code_review).

The gate is unchanged. These tests pin that the rules it blocks on are the
rules the model is told, from the one contract both read.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import NAMESPACE_DNS, UUID, uuid4, uuid5

import pytest

from omnimarket.delegation.acceptance_directives import (
    acceptance_rule_names,
    compose_user_prompt_with_output_directives,
    render_acceptance_directives,
)
from omnimarket.delegation.response_contract_instruction import (
    render_extraction_marker_instruction,
)
from omnimarket.inference.task_class_authority import (
    EnumQualityRuleEnforcement,
    resolve_quality_rule,
)
from omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow import (
    HandlerDelegationWorkflow,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_request import (
    ModelDelegationRequest,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_inference_intent import (
    ModelInferenceIntent,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers import (
    handler_delegation_routing,
)
from omnimarket.nodes.node_delegation_routing_reducer.models.model_routing_decision import (
    ModelRoutingDecision,
)

# The three rules the lab run showed a correct local answer failing on, with no
# request anywhere in what the model received that it satisfy them.
_TEXTUAL_BLOCKING_RULES = (
    "cites_sources",
    "cites_specific_lines",
    "methodical_analysis",
)

_RESEARCH_DOD_HEURISTIC = (
    "no_refusal",
    "cites_sources",
    "methodical_analysis",
    "semantic_adequacy",
    "identifiers_grounded",
)
_CODE_REVIEW_DOD_HEURISTIC = (
    "cites_specific_lines",
    "semantic_adequacy",
    "identifiers_grounded",
)


def _backend_id(name: str) -> UUID:
    return uuid5(NAMESPACE_DNS, f"omninode.ai/backends/{name}")


def _directive(rule: str) -> str:
    declared = resolve_quality_rule(rule)
    assert declared is not None, rule
    assert declared.model_directive, rule
    return declared.model_directive


def _first_inference_intent(
    *,
    task_type: str,
    prompt: str,
    dod_heuristic: tuple[str, ...],
    acceptance_criteria: tuple[str, ...] = ("response_non_empty",),
    quality_contract_mode: str = "extend_task_class",
) -> ModelInferenceIntent:
    handler = HandlerDelegationWorkflow()
    correlation_id = uuid4()
    handler.handle_delegation_request(
        ModelDelegationRequest(
            prompt=prompt,
            task_type=task_type,
            correlation_id=correlation_id,
            emitted_at=datetime.now(UTC),
            quality_contract_mode=quality_contract_mode,
            acceptance_criteria=acceptance_criteria,
        )
    )
    intents = handler.handle_routing_decision(
        ModelRoutingDecision(
            correlation_id=correlation_id,
            task_type=task_type,
            selected_model="Qwen3.8-27B",
            selected_backend_id=_backend_id("local-heavy-reasoning"),
            endpoint_url="http://test-llm:8000/v1/chat/completions",
            cost_tier="low",
            max_context_tokens=8192,
            max_tokens=65536,
            system_prompt="You are an assistant.",
            rationale="test",
            dod_deterministic=("response_non_empty",),
            dod_heuristic=dod_heuristic,
        )
    )
    inference = [i for i in intents if isinstance(i, ModelInferenceIntent)]
    assert len(inference) == 1, intents
    return inference[0]


@pytest.mark.unit
@pytest.mark.parametrize("rule", _TEXTUAL_BLOCKING_RULES)
def test_a_blocking_textual_rule_declares_what_to_tell_the_model(rule: str) -> None:
    """The contract that makes a rule a veto also says what to ask for."""
    declared = resolve_quality_rule(rule)

    assert declared is not None
    assert declared.enforcement is EnumQualityRuleEnforcement.BLOCKING
    assert declared.model_directive
    assert declared.model_directive == declared.model_directive.strip()


@pytest.mark.unit
def test_research_intent_states_its_blocking_rules_in_the_user_turn() -> None:
    intent = _first_inference_intent(
        task_type="research",
        prompt="Summarize the tradeoffs between optimistic and pessimistic locking.",
        dod_heuristic=_RESEARCH_DOD_HEURISTIC,
    )

    assert _directive("cites_sources") in intent.prompt
    assert _directive("methodical_analysis") in intent.prompt
    # The rule set is the gate's own: nothing the class does not block on.
    assert _directive("cites_specific_lines") not in intent.prompt
    # The directives follow the caller's question rather than replacing it.
    assert intent.prompt.index("optimistic and pessimistic") < intent.prompt.index(
        _directive("cites_sources")
    )
    # The system prompt carries no copy: the measured lever is the user turn.
    assert _directive("cites_sources") not in (intent.system_prompt or "")


@pytest.mark.unit
def test_code_review_intent_asks_for_the_line_citation_it_is_graded_on() -> None:
    intent = _first_inference_intent(
        task_type="code_review",
        prompt="Review this snippet: `def run(cmd): import os; os.system(cmd)`",
        dod_heuristic=_CODE_REVIEW_DOD_HEURISTIC,
    )

    assert _directive("cites_specific_lines") in intent.prompt
    assert _directive("cites_sources") not in intent.prompt


@pytest.mark.unit
def test_a_text_shape_marker_sentence_opens_the_user_turn() -> None:
    """I1: the marker asked for only in the system prompt was dropped 6 of 8.

    Restated before the prompt it was carried 8 of 8; restated after it, a
    vague code request came back as a bullet list, so the placement is pinned.
    """
    prompt = "Write a one-sentence description of what a JSON file is."
    intent = _first_inference_intent(
        task_type="document",
        prompt=prompt,
        dod_heuristic=("no_refusal", "accurate", "semantic_adequacy"),
    )

    instruction = intent.response_contract_instruction
    assert instruction is not None
    marker_sentence = render_extraction_marker_instruction("### ANSWER")
    user_turn = intent.prompt.removeprefix("/no_think\n")
    assert user_turn.startswith(marker_sentence)
    assert user_turn.index(marker_sentence) < user_turn.index(prompt)
    # Only the marker sentence is restated, not the Markdown-only instruction.
    assert "Respond with only the requested" not in intent.prompt
    # Still conveyed in full where the evidence reads it, so `conveyed` holds.
    assert instruction in (intent.system_prompt or "")


@pytest.mark.unit
def test_replace_task_class_states_only_the_callers_criteria() -> None:
    intent = _first_inference_intent(
        task_type="research",
        prompt="List two tradeoffs.",
        dod_heuristic=_RESEARCH_DOD_HEURISTIC,
        acceptance_criteria=("response_non_empty", "cites_specific_lines"),
        quality_contract_mode="replace_task_class",
    )

    assert _directive("cites_specific_lines") in intent.prompt
    assert _directive("cites_sources") not in intent.prompt
    assert _directive("methodical_analysis") not in intent.prompt


@pytest.mark.unit
def test_rule_selection_mirrors_the_gate_union_and_order() -> None:
    names = acceptance_rule_names(
        dod_deterministic=("response_non_empty",),
        dod_heuristic=("cites_sources", "methodical_analysis"),
        acceptance_criteria=("methodical_analysis", "cites_specific_lines"),
        quality_contract_mode="extend_task_class",
    )

    assert names == (
        "response_non_empty",
        "cites_sources",
        "methodical_analysis",
        "cites_specific_lines",
    )


@pytest.mark.unit
def test_rules_without_a_directive_or_not_blocking_render_nothing() -> None:
    assert render_acceptance_directives(()) is None
    # `concise` is scored, not blocking: a caller is not told a gradient is a veto.
    assert render_acceptance_directives(("concise", "response_non_empty")) is None
    # An undeclared name has no directive to render.
    assert render_acceptance_directives(("no_such_rule_anywhere",)) is None
    rendered = render_acceptance_directives(("cites_sources", "cites_sources"))
    assert rendered is not None
    assert rendered.count(_directive("cites_sources")) == 1


@pytest.mark.unit
def test_compose_leaves_a_prompt_with_nothing_to_state_byte_identical() -> None:
    assert (
        compose_user_prompt_with_output_directives(
            prompt="Say hi.",
            acceptance_directives=None,
            text_shape_instruction=None,
        )
        == "Say hi."
    )


@pytest.mark.unit
def test_research_system_prompt_no_longer_asks_for_code_line_citations() -> None:
    """OMN-13354 moved research from `cites_specific_lines` to `cites_sources`;
    the class's system prompt kept asking for the retired one."""
    research_prompt = handler_delegation_routing._SYSTEM_PROMPTS["research"]

    assert "specific lines" not in research_prompt
