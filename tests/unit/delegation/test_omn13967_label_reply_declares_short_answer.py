# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-13967: a prompt that asks for only the label declares a short answer.

omnimarket#2915 moved the acceptance authority for a lone token from the
provider's ``finish_reason`` to the request: a prompt that declares its answer
shape through ``response_shape_directives`` has its heuristic band replaced by
``short_form_adequacy``. The directive set named ``say <word>`` and "one word"
but not a label. A classification request such as "Classify the sentiment ...
Reply with only the label." resolved ``unconstrained``, was graded against the
prose rubric, and its correct one-token answer (``positive``) was refused as a
bare single-word fragment, which is the force-escalation the ticket names.

A prompt that merely mentions a label declares nothing, so a one-word reply to a
prose request is still refused.
"""

from __future__ import annotations

from uuid import UUID

import pytest

from omnimarket.enums.enum_requested_response_shape import EnumRequestedResponseShape
from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate import (
    delta,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.models import (
    ModelQualityGateInput,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
    resolve_requested_shape_for_prompt,
    resolve_task_class_dod_checks,
)

pytestmark = pytest.mark.unit

_CORRELATION_ID = UUID("5d1c0c1e-6f0e-4a56-9a1e-0d13967a11ab")
_SINGLE_WORD_RULE_TEXT = "bare single-word fragment"

_LABEL_PROMPTS = [
    "Classify the sentiment of 'I love it' as positive, negative or neutral. "
    "Reply with only the label.",
    "Classify the sentiment: 'I love it'. Answer with just the label.",
    "Classify this log line (INFO, WARN, ERROR): disk full. "
    "Respond with exactly one label.",
    "Is this ticket a bug, a feature or a question? 'app crashes'. "
    "Output only the category.",
    "Which severity applies to 'disk full'? Answer with one of: low, medium, high.",
    "Return only the label for this review: 'great phone'",
]

_PROSE_PROMPTS = [
    "Write the README section that explains how the quality gate labels answers.",
    "Classify the sentiment of this review and explain the label you chose.",
    "Reply with the label and a one paragraph justification.",
    "Explain why the label would be positive here.",
]


def _gate_input(task_type: str, prompt: str, content: str) -> ModelQualityGateInput:
    dod_deterministic, dod_heuristic = resolve_task_class_dod_checks(
        task_type, prompt=prompt
    )
    return ModelQualityGateInput(
        correlation_id=_CORRELATION_ID,
        task_type=task_type,
        llm_response_content=content,
        dod_deterministic=dod_deterministic,
        dod_heuristic=dod_heuristic,
    )


def _refused_by_single_word_rule(reasons: tuple[str, ...]) -> bool:
    return any(_SINGLE_WORD_RULE_TEXT in reason for reason in reasons)


@pytest.mark.parametrize("prompt", _LABEL_PROMPTS)
def test_a_prompt_asking_for_only_the_label_declares_a_single_word(
    prompt: str,
) -> None:
    assert (
        resolve_requested_shape_for_prompt(prompt)
        is EnumRequestedResponseShape.SINGLE_WORD
    )


@pytest.mark.parametrize("prompt", _PROSE_PROMPTS)
def test_a_prompt_that_only_mentions_a_label_declares_nothing(prompt: str) -> None:
    assert (
        resolve_requested_shape_for_prompt(prompt)
        is EnumRequestedResponseShape.UNCONSTRAINED
    )


@pytest.mark.parametrize("task_type", ["reasoning", "review"])
@pytest.mark.parametrize("prompt", _LABEL_PROMPTS)
@pytest.mark.parametrize("label", ["positive", "ERROR"])
def test_a_correct_one_token_label_is_not_refused_as_a_fragment(
    task_type: str, prompt: str, label: str
) -> None:
    result = delta(_gate_input(task_type, prompt, label))
    assert not _refused_by_single_word_rule(result.failure_reasons), (
        result.failure_reasons
    )


@pytest.mark.parametrize("prompt", _PROSE_PROMPTS)
def test_a_one_token_reply_to_a_prose_request_is_still_refused(prompt: str) -> None:
    result = delta(_gate_input("document", prompt, "positive"))
    assert _refused_by_single_word_rule(result.failure_reasons), result.failure_reasons


@pytest.mark.parametrize("content", ["", "   ", "The label is", "positive,"])
def test_a_declared_label_shape_does_not_excuse_a_cut_off_reply(
    content: str,
) -> None:
    result = delta(_gate_input("reasoning", _LABEL_PROMPTS[0], content))
    assert not result.passed, result.failure_reasons
