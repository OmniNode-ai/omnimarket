# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-20844 — a correct short literal answer is not refused as content-free.

Delegation ladder task TN-h05 asks what a function call evaluates to and says,
as its last line, "Reply with only the Python literal it evaluates to". The
right answer is ``(None, None)``. The quality gate's ultra-short pre-pass read
that answer as ``'none none'`` (every token a negation word), vetoed it as a
content-free declination on every rung, and the ladder climbed to the end with
no answer.

Two facts made that reachable:

* the prompt's own shape directive sits after a long program, outside the 400
  characters the resolver scanned, so the request resolved ``unconstrained``;
* even a declared shape kept ``no_refusal``, whose pre-pass has no notion of a
  literal answer.

The relaxation is scoped to a DECLARED shape (the band carries
``short_form_adequacy``) and to a response that parses as a non-string Python
literal. An empty answer, a filler word and a refusal stay refused.
"""

from __future__ import annotations

import uuid

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

_PROGRAM = "\n".join(
    f"def helper_{i}(value):\n    return value + {i}\n" for i in range(40)
)

# A long task whose directive is its LAST line, as in the TN-h05 bundle.
LITERAL_PROMPT = (
    "Here is a Python program.\n\n```python\n"
    + _PROGRAM
    + "```\n\nWhat does `helper_3(None)` evaluate to? Reply with only the Python "
    "literal it evaluates to, exactly as repr() would print it.\n\n"
    "Respond with the answer only. Do not show your reasoning."
)

# The same program, with no directive at all.
UNDECLARED_PROMPT = (
    "Here is a Python program.\n\n```python\n"
    + _PROGRAM
    + "```\n\nWhat does `helper_3(None)` evaluate to? Explain."
)


def _gate(content: str, *, prompt: str) -> object:
    det, heur = resolve_task_class_dod_checks("reasoning", prompt=prompt)
    return delta(
        ModelQualityGateInput(
            correlation_id=str(uuid.uuid4()),
            task_type="reasoning",
            llm_response_content=content,
            dod_deterministic=det,
            dod_heuristic=heur,
        )
    )


def test_directive_in_the_prompt_tail_declares_the_shape() -> None:
    assert len(LITERAL_PROMPT) > 1000
    assert (
        resolve_requested_shape_for_prompt(LITERAL_PROMPT)
        is EnumRequestedResponseShape.EXACT_LITERAL
    )


def test_a_long_prompt_without_a_directive_stays_unconstrained() -> None:
    assert (
        resolve_requested_shape_for_prompt(UNDECLARED_PROMPT)
        is EnumRequestedResponseShape.UNCONSTRAINED
    )


def test_a_directive_buried_mid_prompt_declares_nothing() -> None:
    """Only the instruction framing counts: the head and the tail, not the middle."""
    prompt = (
        "Review this change.\n"
        + "x" * 800
        + "\nReply with exactly: ok\n"
        + "y" * 800
        + "\nThen explain your review."
    )
    assert (
        resolve_requested_shape_for_prompt(prompt)
        is EnumRequestedResponseShape.UNCONSTRAINED
    )


def test_whole_prompt_anchored_directive_is_not_matched_in_a_tail() -> None:
    """``say ok`` only declares a shape when it IS the whole prompt."""
    prompt = "Write the release notes.\n" + "z" * 800 + "\nsay ok"
    assert (
        resolve_requested_shape_for_prompt(prompt)
        is EnumRequestedResponseShape.UNCONSTRAINED
    )


@pytest.mark.parametrize(
    "answer",
    [
        "(None, None)",
        "None",
        "False",
        "0",
        "42",
        "-1.5",
        "(1, 2)",
        "[]",
        "{'a': None}",
        "```python\n(None, None)\n```",
    ],
)
def test_a_literal_matching_the_declared_shape_is_not_content_free(
    answer: str,
) -> None:
    result = _gate(answer, prompt=LITERAL_PROMPT)
    assert result.passed is True, result.failure_reasons
    assert not any("content-free" in r for r in result.failure_reasons)


@pytest.mark.parametrize(
    "answer",
    [
        "",
        "   ",
        "no",
        "No.",
        "none none",
        "None.",
        "N/A",
        "'none'",
        "nothing",
        "I cannot answer that.",
    ],
)
def test_empty_or_filler_is_still_refused_under_the_declared_shape(
    answer: str,
) -> None:
    result = _gate(answer, prompt=LITERAL_PROMPT)
    assert result.passed is False, answer


def test_an_undeclared_prompt_still_refuses_the_negation_only_answer() -> None:
    """The relaxation is scoped to a declared shape, not granted to every class."""
    result = _gate("(None, None)", prompt=UNDECLARED_PROMPT)
    assert result.passed is False
    assert any("content-free" in r for r in result.failure_reasons)
