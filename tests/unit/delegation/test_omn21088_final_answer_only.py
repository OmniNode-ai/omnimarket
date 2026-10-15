# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-21088: ``result.response`` is the final answer only.

Measured 2026-10-14 (omnimarket 0.4.313, prompt "what is 2+2", model
qwen/qwen3.8-27b on OpenRouter): ``result.response`` was about 990 bytes of
sourced reasoning ("According to ISO 80000-2 ... because ... therefore ... the
tradeoff is ...") with a References list, ``raw_response.text`` opened
``"\\n\\n### ANSWER\\n"``, and ``output_tokens`` was 1290 for about 250 tokens of
visible text. Three things caused it, and each has a test here:

* the ``research`` class told the model to cite sources and give reasoning and
  tradeoffs for every prompt, and the gate vetoed an answer without them;
* a bare correct answer ("4") failed ``semantic_adequacy`` as a one-word fragment;
* OpenRouter received only ``chat_template_kwargs.enable_thinking``, which it
  does not read, so the model reasoned on the bill.

The response seam also strips reasoning blocks and the answer header so a
caller never receives either, whatever the provider sent.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from omnimarket.delegation.acceptance_directives import (
    acceptance_rule_names,
    render_acceptance_directives,
)
from omnimarket.delegation.deliverable_extraction import final_answer_text
from omnimarket.inference.protocol_config import apply_inference_protocol
from omnimarket.models.delegation.wire.model_delegate_skill_request import (
    ModelDelegateSkillRequest,
)
from omnimarket.models.delegation.wire.model_delegate_skill_response import (
    ModelDelegateSkillResponse,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.handler_delegate_skill import (
    _response_from_result,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_local_delegation_dispatch import (
    _TASK_TYPE_SYSTEM_PROMPTS,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate import (
    delta as quality_gate_delta,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_quality_gate_input import (
    ModelQualityGateInput,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
    _SYSTEM_PROMPTS,
    resolve_task_class_dod_checks,
)

pytestmark = pytest.mark.unit

_TASK_CLASSES = (
    "code_generation",
    "refactor",
    "documentation",
    "document",
    "research",
    "test",
    "validator_generation",
    "reasoning",
    "complex_reasoning",
    "planning",
    "review",
    "code_review",
    "summarization",
    "agent_delegation",
    "escalation",
)

#: The rules whose directive asks the model to write out reasoning, sources or
#: tradeoffs into its answer.
_REASONING_PROSE_RULES = frozenset(
    {"cites_sources", "methodical_analysis", "step_by_step_explanation"}
)


# --- 1. the response seam -------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "answer"),
    [
        ("\n\n### ANSWER\n4\n\n", "4"),
        ("### ANSWER\n\n4", "4"),
        ("=== ANSWER ===\n4\n", "4"),
        ("**### Answer:**\n4", "4"),
        ("<think>\n2 plus 2 is 4.\n</think>\n\n4", "4"),
        ("<think>2 plus 2 is 4.</think>\n\n### ANSWER\n4\n", "4"),
        ("Okay, the user asks 2+2.\n</think>\n\n4", "4"),
        ("<answer>\n4\n</answer>", "4"),
        ("\n\n4\n\n", "4"),
    ],
)
def test_the_response_is_the_answer_with_no_header_reasoning_or_blank_lines(
    raw: str, answer: str
) -> None:
    assert final_answer_text(raw)[0] == answer


def test_removed_reasoning_is_kept_apart_from_the_answer() -> None:
    answer, reasoning = final_answer_text("<think>2 plus 2 is 4.</think>\n\n4")
    assert answer == "4"
    assert "2 plus 2 is 4." in reasoning


def test_a_header_inside_the_answer_is_kept() -> None:
    """Only a header that opens the response is the answer marker."""
    raw = "Intro line.\n\n### ANSWER\nBody"
    assert final_answer_text(raw)[0] == raw


def test_indentation_of_the_first_answer_line_is_kept() -> None:
    raw = "\n\n    def f() -> int:\n        return 4\n"
    assert final_answer_text(raw)[0] == "    def f() -> int:\n        return 4"


def test_a_clean_answer_is_returned_unchanged() -> None:
    assert final_answer_text("The answer is 4.") == ("The answer is 4.", "")


def _response(content: str) -> tuple[str, str]:
    response = _response_from_result(
        ModelDelegateSkillRequest(
            prompt="what is 2+2",
            task_type="research",
            source="claude-code",
            correlation_id=uuid4(),
        ),
        {"status": "completed", "content": content, "quality_gate_passed": True},
        tenant_id=None,
        queue_wait_ms=None,
        execution_duration_ms=10,
        budget_evidence=None,
    )
    return response.response, response.response_reasoning


def test_the_skill_response_carries_only_the_final_answer() -> None:
    response, reasoning = _response("<think>2 plus 2.</think>\n\n### ANSWER\n4\n")
    assert response == "4"
    assert "2 plus 2." in reasoning


def test_the_skill_response_of_a_clean_answer_has_no_reasoning_field_value() -> None:
    assert _response("4") == ("4", "")


def test_reasoning_is_accepted_without_emitting_a_new_wire_key() -> None:
    response = ModelDelegateSkillResponse.model_validate(
        {
            "status": "completed",
            "correlation_id": uuid4(),
            "task_type": "research",
            "response": "4",
            "response_reasoning": "2 plus 2.",
        }
    )
    assert response.response_reasoning == "2 plus 2."
    assert "response_reasoning" not in response.model_dump(mode="json")
    assert "response_reasoning" not in response.model_json_schema()["properties"]
    decoded = ModelDelegateSkillResponse.model_validate_json(response.model_dump_json())
    assert decoded.response == "4"


def test_removed_reasoning_is_serialized_on_the_deciding_attempt() -> None:
    response = _response_from_result(
        ModelDelegateSkillRequest(
            prompt="what is 2+2",
            task_type="research",
            source="claude-code",
            correlation_id=uuid4(),
        ),
        {
            "status": "completed",
            "content": "<think>2 plus 2.</think>\n### ANSWER\n4",
            "quality_gate_passed": True,
            "attempts": [
                {
                    "tier": "local",
                    "backend_id": "local-coder",
                    "model_id": "model",
                    "quality_gate_passed": True,
                    "acceptance_decision": "accept",
                    "reasoning_preamble": "Earlier recorded preamble.",
                }
            ],
        },
        tenant_id=None,
        queue_wait_ms=None,
        execution_duration_ms=10,
        budget_evidence=None,
    )
    decoded = ModelDelegateSkillResponse.model_validate_json(response.model_dump_json())
    assert decoded.response == "4"
    assert "2 plus 2." in decoded.attempts[0].reasoning_preamble
    assert "Earlier recorded preamble." in decoded.attempts[0].reasoning_preamble


# --- 2. no class asks for reasoning the prompt did not ask for -------------------


@pytest.mark.parametrize("task_type", _TASK_CLASSES)
def test_a_short_question_is_not_asked_for_reasoning_sources_or_tradeoffs(
    task_type: str,
) -> None:
    deterministic, heuristic = resolve_task_class_dod_checks(
        task_type, prompt="what is 2+2"
    )
    assert _REASONING_PROSE_RULES.isdisjoint(heuristic)
    rendered = render_acceptance_directives(
        acceptance_rule_names(
            dod_deterministic=deterministic,
            dod_heuristic=heuristic,
            acceptance_criteria=(),
            quality_contract_mode="extend_task_class",
        )
    )
    for phrase in ("References", "tradeoff", "numbered steps", "because"):
        assert phrase not in (rendered or "")


@pytest.mark.parametrize(
    ("prompt", "rule"),
    [
        ("What is MVCC in PostgreSQL? Cite your sources.", "cites_sources"),
        ("What is MVCC? Include references.", "cites_sources"),
        ("What are the tradeoffs of MVCC versus locking?", "methodical_analysis"),
        ("Explain why MVCC avoids read locks.", "methodical_analysis"),
    ],
)
def test_a_prompt_that_asks_for_them_keeps_the_rule(prompt: str, rule: str) -> None:
    """Negative control: the rules are narrowed to prompts asking, not dropped."""
    assert rule in resolve_task_class_dod_checks("research", prompt=prompt)[1]


def test_a_prompt_that_asks_for_steps_keeps_the_step_rule() -> None:
    heuristic = resolve_task_class_dod_checks(
        "reasoning", prompt="Reason through this step by step: is 91 prime?"
    )[1]
    assert "step_by_step_explanation" in heuristic


@pytest.mark.parametrize("task_type", sorted(_SYSTEM_PROMPTS))
def test_no_bus_system_prompt_asks_for_reasoning_prose(task_type: str) -> None:
    text = _SYSTEM_PROMPTS[task_type].lower()
    for phrase in ("step by step", "step-by-step", "attribute your claims", "thorough"):
        assert phrase not in text


@pytest.mark.parametrize("task_type", sorted(_TASK_TYPE_SYSTEM_PROMPTS))
def test_no_local_system_prompt_asks_for_reasoning_prose(task_type: str) -> None:
    text = _TASK_TYPE_SYSTEM_PROMPTS[task_type].lower()
    for phrase in ("step by step", "step-by-step", "thoroughly", "detailed"):
        assert phrase not in text


# --- 3. providers are asked not to return reasoning ------------------------------


@pytest.mark.parametrize("task_type", sorted(set(_TASK_CLASSES) - {"reasoning"}))
def test_openrouter_is_asked_for_no_reasoning(task_type: str) -> None:
    _, _, options = apply_inference_protocol(
        system_prompt="s", prompt="p", model="qwen/qwen3.8-27b", task_type=task_type
    )
    assert options["reasoning"] == {"effort": "minimal", "exclude": True}


def test_openrouter_reasoning_class_keeps_low_effort_reasoning_but_excludes_it() -> (
    None
):
    _, _, options = apply_inference_protocol(
        system_prompt="s", prompt="p", model="qwen/qwen3.8-27b", task_type="reasoning"
    )
    assert options["reasoning"] == {"effort": "low", "exclude": True}


def test_a_local_model_id_receives_no_openrouter_reasoning_option() -> None:
    _, _, options = apply_inference_protocol(
        system_prompt="s", prompt="p", model="Qwen3.8-27B", task_type="research"
    )
    assert "reasoning" not in options


# --- 4. a correct one-line answer clears the gate ---------------------------------


def _gate(task_type: str, prompt: str, response: str) -> tuple[bool, tuple[str, ...]]:
    deterministic, heuristic = resolve_task_class_dod_checks(task_type, prompt=prompt)
    result = quality_gate_delta(
        ModelQualityGateInput(
            correlation_id=uuid4(),
            task_type=task_type,
            llm_response_content=response,
            dod_deterministic=deterministic,
            dod_heuristic=heuristic,
        ),
        grounding_source=prompt,
    )
    return result.passed, result.failure_reasons


@pytest.mark.parametrize("answer", ["4", "4.", "2 + 2 = 4."])
def test_a_correct_one_line_answer_to_a_short_question_passes(answer: str) -> None:
    passed, reasons = _gate("research", "what is 2+2", answer)
    assert passed is True, reasons


def test_a_one_token_answer_to_a_one_line_reasoning_question_passes() -> None:
    """Measured on the branch: "5pm" was refused at a 12-word question bound."""
    passed, reasons = _gate(
        "reasoning",
        "If a train leaves at 3pm and travels for 2 hours, what time does it arrive?",
        "5pm",
    )
    assert passed is True, reasons


def test_a_bare_word_answering_an_open_request_is_still_refused() -> None:
    """Negative control: the one-word rule still holds where no question was asked."""
    passed, _ = _gate(
        "research", "Write an overview of how MVCC works in PostgreSQL.", "MVCC"
    )
    assert passed is False


@pytest.mark.parametrize("marker", ["", "### ANSWER\n"])
@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("handler_routing: [unfinished block", False),
        ("handler_routing: {unfinished: block", False),
        ("handler_routing: [valid, block]", True),
        ('handler_routing: ["a literal [ inside a string"]', True),
        ("A prose answer can cite [12] without becoming YAML.", True),
    ],
)
def test_research_still_rejects_unfinished_flow_collections(
    marker: str, content: str, expected: bool
) -> None:
    passed, reasons = _gate(
        "research", "write a handler_routing block", marker + content
    )
    assert passed is expected, reasons
