# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""complex_reasoning is refused on a rule the model is never told (OMN-19400).

Dev-lane run d2893ea5-2ac6-44a5-83e3-46c305c33daa (2026-09-24, task class
``complex_reasoning``): the local rung (local-heavy-reasoning, Qwen3.8-27B)
was selected and answered three times, and each answer was refused with
``TASK_MISMATCH: failed step_by_step_explanation``. The ladder then climbed to
the ``claude`` ceiling slot, which is bound to free-tier Gemini, and died on a
429. No complex_reasoning delegation had ever completed.

``step_by_step_explanation`` is a BLOCKING heuristic in the ``complex_reasoning``
and ``reasoning`` definitions of done, and the gate checks it by vocabulary: the
answer must contain ``step``, ``1.``, ``first`` or ``then``. OMN-18349 made each
blocking rule tell the model what it requires through
``quality_rules.<rule>.model_directive``, stated in the user turn. This rule was
never given one, so the only instruction the model received about reasoning was
the Markdown output instruction, which says not to include any. The model
obeyed, and was refused for it.

These tests pin that every vocabulary-checked blocking rule of the two
reasoning classes is stated to the model, from the one contract the gate reads.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import NAMESPACE_DNS, UUID, uuid4, uuid5

import pytest

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
from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate import (
    _HEURISTIC_CONTAINS_ANY_CHECKS,
    _apply_heuristic_check,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
    resolve_task_class_dod_checks,
)
from omnimarket.nodes.node_delegation_routing_reducer.models.model_routing_decision import (
    ModelRoutingDecision,
)

_REASONING_CLASSES = ("complex_reasoning", "reasoning")

# The d2893ea5 prompt, verbatim.
_SHEEP_PROMPT = (
    "A farmer has 17 sheep. All but 9 die. How many sheep are left? "
    "Give the number and a one-sentence reason."
)


def _backend_id(name: str) -> UUID:
    return uuid5(NAMESPACE_DNS, f"omninode.ai/backends/{name}")


def _directive(rule: str) -> str:
    declared = resolve_quality_rule(rule)
    assert declared is not None, rule
    assert declared.model_directive, rule
    return declared.model_directive


def _vocabulary_checked_blocking_rules(task_type: str) -> tuple[str, ...]:
    """The class's blocking heuristics that the gate decides by marker words."""
    _deterministic, heuristic = resolve_task_class_dod_checks(task_type)
    rules: list[str] = []
    for rule in heuristic:
        if rule not in _HEURISTIC_CONTAINS_ANY_CHECKS:
            continue
        declared = resolve_quality_rule(rule)
        # An undeclared rule is blocking by the gate's fail-closed default.
        if (
            declared is None
            or declared.enforcement is EnumQualityRuleEnforcement.BLOCKING
        ):
            rules.append(rule)
    return tuple(rules)


def _first_inference_intent(*, task_type: str, prompt: str) -> ModelInferenceIntent:
    """Build the first local inference intent the bus orchestrator emits."""
    deterministic, heuristic = resolve_task_class_dod_checks(task_type, prompt)
    handler = HandlerDelegationWorkflow()
    correlation_id = uuid4()
    handler.handle_delegation_request(
        ModelDelegationRequest(
            prompt=prompt,
            task_type=task_type,
            correlation_id=correlation_id,
            emitted_at=datetime.now(UTC),
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
            dod_deterministic=deterministic,
            dod_heuristic=heuristic,
        )
    )
    inference = [i for i in intents if isinstance(i, ModelInferenceIntent)]
    assert len(inference) == 1, intents
    return inference[0]


@pytest.mark.unit
def test_step_by_step_explanation_declares_a_model_directive() -> None:
    declared = resolve_quality_rule("step_by_step_explanation")

    assert declared is not None
    assert declared.enforcement is EnumQualityRuleEnforcement.BLOCKING
    assert declared.model_directive
    assert declared.model_directive == declared.model_directive.strip()


@pytest.mark.unit
@pytest.mark.parametrize("task_type", _REASONING_CLASSES)
def test_marker_rules_are_stated_for_the_reasoning_classes(task_type: str) -> None:
    """Every vocabulary veto on the class carries the sentence that asks for it."""
    rules = _vocabulary_checked_blocking_rules(task_type)

    # Positive control: the class really is gated on step_by_step_explanation,
    # so an empty selection cannot pass this test by vacuity.
    assert "step_by_step_explanation" in rules
    unstated = []
    for rule in rules:
        declared = resolve_quality_rule(rule)
        if declared is None or not declared.model_directive:
            unstated.append(rule)
    assert unstated == []


@pytest.mark.unit
def test_complex_reasoning_user_turn_asks_for_the_step_by_step_explanation() -> None:
    intent = _first_inference_intent(
        task_type="complex_reasoning", prompt=_SHEEP_PROMPT
    )

    assert _directive("step_by_step_explanation") in intent.prompt
    assert _directive("methodical_analysis") in intent.prompt
    # The caller's question stays ahead of the requirements, unchanged.
    assert intent.prompt.index(_SHEEP_PROMPT) < intent.prompt.index(
        _directive("step_by_step_explanation")
    )


@pytest.mark.unit
def test_an_answer_that_follows_the_directive_meets_the_rule() -> None:
    """The directive asks for what the gate checks, not something adjacent."""
    directive = _directive("step_by_step_explanation")
    _category, markers = _HEURISTIC_CONTAINS_ANY_CHECKS["step_by_step_explanation"]
    # The directive names a marker the gate accepts, in the form it accepts it.
    assert any(marker in directive.lower() for marker in markers)

    followed = (
        "1. All but 9 die means exactly 9 sheep do not die.\n"
        "2. Therefore 9 sheep are left, because only the others died.\n\n"
        "9 sheep are left."
    )
    ignored = "9 sheep are left."

    assert _apply_heuristic_check("step_by_step_explanation", followed) is None
    # Negative control: a bare answer with none of the accepted markers
    # (including the causal connectives OMN-19401 added) is still refused,
    # so the gate was not loosened into a no-op.
    assert not any(marker in ignored.lower() for marker in markers)
    assert _apply_heuristic_check("step_by_step_explanation", ignored) is not None
