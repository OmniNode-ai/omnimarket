# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Regression coverage for markdown fences inside delegated Python strings."""

from __future__ import annotations

from uuid import uuid4

import pytest

from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate import (
    _extract_fenced_code_blocks,
    _extract_fenced_code_blocks_with_lang,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate import (
    delta as quality_gate_delta,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_quality_gate_input import (
    ModelQualityGateInput,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_quality_gate_result import (
    ModelQualityGateResult,
)

pytestmark = pytest.mark.unit


def _check(content: str, *checks: str) -> ModelQualityGateResult:
    return quality_gate_delta(
        ModelQualityGateInput(
            correlation_id=uuid4(),
            task_type="code_generation",
            llm_response_content=content,
            dod_deterministic=checks,
            dod_heuristic=(),
        )
    )


@pytest.mark.parametrize("check", ["compiles_without_errors", "final_artifact_only"])
def test_backticks_inside_python_string_pass(check: str) -> None:
    answer = '```python\nx = "```"\nprint(x)\n```'

    result = _check(answer, check)

    assert result.passed, result.failure_reasons
    assert result.failure_reasons == ()
    assert result.skipped_checks == ()


def test_two_python_fences_remain_separate_blocks() -> None:
    # The future import is valid in its own block, but fails if the bodies
    # are joined after the assignment in the first block.
    bodies = ["x = 1\n", "from __future__ import annotations\ny = 2\n"]
    answer = "\n\n".join(f"```python\n{body}```" for body in bodies)

    assert _extract_fenced_code_blocks(answer) == bodies
    assert _extract_fenced_code_blocks_with_lang(answer) == [
        ("python", body) for body in bodies
    ]
    result = _check(answer, "compiles_without_errors", "final_artifact_only")
    assert result.passed, result.failure_reasons
    assert result.failure_reasons == ()
    assert result.skipped_checks == ()


@pytest.mark.parametrize("valid_prefix", ["", "```python\nx = 1\n```\n\n"])
def test_unterminated_python_string_still_fails(valid_prefix: str) -> None:
    answer = valid_prefix + '```python\nx = "unterminated\n```'

    result = _check(answer, "compiles_without_errors")

    assert not result.passed
    assert any(
        "does not compile as Python: unterminated string literal" in reason
        for reason in result.failure_reasons
    )
    assert result.skipped_checks == ()


@pytest.mark.parametrize("indent", ["", " ", "  ", "   "])
@pytest.mark.parametrize("trailing", ["", " \t "])
@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_closing_fence_accepts_whitespace(
    indent: str, trailing: str, newline: str
) -> None:
    body = f'x = "```"{newline}print(x){newline}'
    answer = f"```python{newline}{body}{indent}```{trailing}{newline}"

    assert _extract_fenced_code_blocks(answer) == [body]
    assert _extract_fenced_code_blocks_with_lang(answer) == [("python", body)]
    result = _check(answer, "compiles_without_errors", "final_artifact_only")
    assert result.passed, result.failure_reasons
    assert result.failure_reasons == ()
    assert result.skipped_checks == ()
