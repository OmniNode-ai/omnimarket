# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# Copyright (c) 2026 OmniNode Team
"""A usage example in its own code block does not fail the code gate (OMN-21258).

Receipt 3861ec7f, from an onboarding walk: the model returned a correct
``count_emails_by_sender`` in one code block, a ``**Usage:**`` label, and a
second block that calls it. Both attempts were vetoed. ``names_resolve`` read
each block on its own, so the call in the second block named a function bound
"nowhere", and ``final_artifact_only`` counted the label as prose.

The fix reads the answer the way a person runs it: a name bound in any of its
Python blocks is bound for all of them, and one short label line between two
blocks is a caption, not deliberation. Prose before the first block, after the
last one, or a full sentence between two blocks is still refused, and a name
nothing binds still vetoes.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from omnimarket.delegation.content_grounding import (
    evaluate_name_resolution,
    resolve_name_resolution_policy,
)
from omnimarket.models.delegation.wire.model_named_test_run import ModelNamedTestRun
from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate import (
    delta,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_quality_gate_input import (
    ModelQualityGateInput,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
    resolve_task_class_dod_checks,
)

pytestmark = pytest.mark.unit

_FENCE = "`" * 3
_PASSED_RUN = ModelNamedTestRun(
    test_ids=("tests/test_count.py::test_count",), exit_code=0
)
# The prompt never names the function, so the name can only resolve from the
# answer's own first block.
_SOURCE = (
    "Write a function that counts how many emails each sender sent, from a JSON string."
)
_PROSE = "non-artifact prose"

_DEFINITION = (
    "import json\n"
    "from collections import Counter\n"
    "from typing import Any\n"
    "\n"
    "\n"
    "def count_emails_by_sender(json_data: str) -> dict[str, int]:\n"
    '    """Return a total count of emails sent by each person from a JSON string.\n'
    "\n"
    "    Args:\n"
    "        json_data: A JSON string representing a list of email objects.\n"
    '                   Each object must contain a "sender" key.\n'
    "\n"
    "    Returns:\n"
    "        A dictionary mapping each sender to the number of emails they sent.\n"
    "\n"
    "    Raises:\n"
    "        json.JSONDecodeError: If the input is not valid JSON.\n"
    "        TypeError: If the parsed JSON is not a list.\n"
    '        KeyError: If an email object is missing the "sender" key.\n'
    "\n"
    "    Example:\n"
    "        >>> data = json.dumps([\n"
    '        ...     {"sender": "alice@example.com", "to": "bob@example.com"},\n'
    '        ...     {"sender": "bob@example.com", "to": "alice@example.com"},\n'
    '        ...     {"sender": "alice@example.com", "to": "carol@example.com"},\n'
    "        ... ])\n"
    "        >>> count_emails_by_sender(data)\n"
    "        {'alice@example.com': 2, 'bob@example.com': 1}\n"
    '    """\n'
    "    emails: list[dict[str, Any]] = json.loads(json_data)\n"
    "\n"
    "    if not isinstance(emails, list):\n"
    '        raise TypeError(f"Expected a JSON list, got {type(emails).__name__}")\n'
    "\n"
    '    counter = Counter(email["sender"] for email in emails)\n'
    "    return dict(counter)\n"
)
_USAGE = (
    "import json\n"
    "\n"
    "data = json.dumps([\n"
    '    {"sender": "alice@example.com", "to": "bob@example.com", "subject": "Hi"},\n'
    '    {"sender": "bob@example.com", "to": "alice@example.com", "subject": "Re: Hi"},\n'
    '    {"sender": "alice@example.com", "to": "carol@example.com", "subject": "Hello"},\n'
    '    {"sender": "carol@example.com", "to": "alice@example.com", "subject": "Hey"},\n'
    "])\n"
    "\n"
    "result = count_emails_by_sender(data)\n"
    "# {'alice@example.com': 2, 'bob@example.com': 1, 'carol@example.com': 1}\n"
)
# Receipt 3861ec7f's response, byte for byte.
RESPONSE = (
    f"{_FENCE}python\n{_DEFINITION}{_FENCE}\n\n**Usage:**\n\n"
    f"{_FENCE}python\n{_USAGE}{_FENCE}"
)


def _code_input(content: str) -> ModelQualityGateInput:
    deterministic, heuristic = resolve_task_class_dod_checks("code_generation")
    return ModelQualityGateInput(
        correlation_id=uuid4(),
        task_type="code_generation",
        llm_response_content=content,
        dod_deterministic=deterministic,
        dod_heuristic=heuristic,
    )


def _reasons(content: str) -> tuple[bool, list[str]]:
    result = delta(
        _code_input(content), grounding_source=_SOURCE, named_test_run=_PASSED_RUN
    )
    return result.passed, list(result.failure_reasons)


def _unresolved(content: str) -> tuple[str, ...]:
    verdict = evaluate_name_resolution(
        content=content,
        grounding_source=_SOURCE,
        policy=resolve_name_resolution_policy(),
    )
    assert verdict.evaluated
    return verdict.unresolved


def test_the_fixture_is_the_receipt_response() -> None:
    assert RESPONSE.count(_FENCE) == 4
    assert "\n\n**Usage:**\n\n" in RESPONSE
    assert "result = count_emails_by_sender(data)" in RESPONSE
    assert "count_emails_by_sender" not in _SOURCE


def test_a_function_defined_in_one_block_resolves_where_another_block_calls_it() -> (
    None
):
    """AC1."""
    assert _unresolved(RESPONSE) == ()
    _, reasons = _reasons(RESPONSE)
    assert not [r for r in reasons if r.startswith("UNGROUNDED:")], reasons


def test_a_usage_label_between_two_blocks_is_not_prose() -> None:
    """AC2."""
    _, reasons = _reasons(RESPONSE)
    assert not [r for r in reasons if _PROSE in r], reasons


@pytest.mark.parametrize(
    "label", ["Usage:", "**Usage:**", "**Usage**:", "### Example usage:"]
)
def test_short_labels_between_blocks_are_captions(label: str) -> None:
    answer = (
        f"{_FENCE}python\ndef f() -> int:\n    return 1\n{_FENCE}\n\n{label}\n\n"
        f"{_FENCE}python\nprint(f())\n{_FENCE}"
    )
    _, reasons = _reasons(answer)
    assert not [r for r in reasons if _PROSE in r], reasons


def test_a_name_no_block_binds_still_vetoes() -> None:
    """AC3: binding across blocks does not bind a name nothing defines."""
    answer = (
        f"{_FENCE}python\ndef f() -> int:\n    return 1\n{_FENCE}\n\nUsage:\n\n"
        f"{_FENCE}python\nprint(f(), g())\n{_FENCE}"
    )
    assert _unresolved(answer) == ("g",)
    passed, reasons = _reasons(answer)
    assert passed is False
    assert any(r.startswith("UNGROUNDED:") and "g" in r for r in reasons), reasons


@pytest.mark.parametrize(
    "answer",
    [
        # deliberation before the artifact
        f"We need to implement the function first.\n\n{_FENCE}python\n{_DEFINITION}{_FENCE}",
        # a sentence after it
        f"{_FENCE}python\n{_DEFINITION}{_FENCE}\n\nThis counts every sender.",
        # a full sentence between two blocks
        f"{_FENCE}python\n{_DEFINITION}{_FENCE}\n\n"
        "The function below reads the JSON and then counts every sender it finds "
        f"in the list.\n\n{_FENCE}python\n{_USAGE}{_FENCE}",
        # a label followed by an explanation is more than one line
        f"{_FENCE}python\n{_DEFINITION}{_FENCE}\n\nUsage:\nCall it with a JSON list.\n\n"
        f"{_FENCE}python\n{_USAGE}{_FENCE}",
    ],
    ids=["before", "after", "sentence-between", "label-plus-text"],
)
def test_prose_outside_a_caption_is_still_refused(answer: str) -> None:
    _, reasons = _reasons(answer)
    assert any(_PROSE in r for r in reasons), reasons
