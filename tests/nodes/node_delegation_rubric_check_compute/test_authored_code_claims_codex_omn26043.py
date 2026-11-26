# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Drafted by Codex through HandlerCodexHarnessAttempt on its account default model (OMN-26043)."""

from types import SimpleNamespace
from typing import cast

import pytest

from omnimarket.nodes.node_delegation_rubric_check_compute.handlers.criteria_tool_use import (
    _authored_code_claims,
)
from omnimarket.nodes.node_delegation_rubric_check_compute.models import (
    ModelTaskAnswerTraceableParams,
)


@pytest.mark.parametrize(
    ("body", "expected_claims"),
    [
        ("seen.add(item)", ""),
        ('seen.add("OMN-26043")', "OMN-26043"),
        ('seen.add("src/handlers/check.py")', "src/handlers/check.py"),
    ],
)
def test_authored_code_claims_preserves_prose_and_keeps_fenced_references(
    body: str, expected_claims: str
) -> None:
    params = SimpleNamespace(ticket_id_pattern=r"\b[A-Z]+-\d+\b")
    before = "Before the fence, `seen.add(item)` is prose.\n\n"
    after = "\n\nAfter the fence, keep this punctuation and spacing.  \n"
    text = f"{before}```python\n{body}\n```{after}"

    assert (
        _authored_code_claims(text, cast(ModelTaskAnswerTraceableParams, params))
        == before + expected_claims + after
    )
