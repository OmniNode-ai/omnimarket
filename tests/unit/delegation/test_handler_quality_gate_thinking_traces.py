# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for thinking trace stripping, markdown fence extraction, and passes_existing_tests support."""

from __future__ import annotations

from uuid import uuid4

import pytest

from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate import (
    _check_compiles_without_errors,
    _extract_fenced_code_blocks,
    _strip_thinking_traces,
    delta,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_quality_gate_input import (
    ModelQualityGateInput,
)

_THINKING_PREFIX = (
    "<think>\n"
    "Here's my reasoning process:\n\n"
    "1. The user wants Python code.\n"
    "2. I should write a function.\n"
    "3. Let me think through edge cases.\n"
    "</think>\n"
)

_CLEAN_CODE = "def add(a: int, b: int) -> int:\n    return a + b\n"
_RESPONSE_WITH_THINKING = _THINKING_PREFIX + _CLEAN_CODE

_FENCED_CODE = "```python\n" + _CLEAN_CODE + "```"
_MIXED_RESPONSE = (
    _THINKING_PREFIX
    + _FENCED_CODE
    + "\nThis function adds two integers and returns the result.\n"
)


@pytest.mark.unit
def test_strip_thinking_traces_removes_think_block() -> None:
    result = _strip_thinking_traces(_RESPONSE_WITH_THINKING)
    assert "<think>" not in result
    assert "</think>" not in result
    assert _CLEAN_CODE in result


@pytest.mark.unit
def test_strip_thinking_traces_noop_on_clean_content() -> None:
    result = _strip_thinking_traces(_CLEAN_CODE)
    assert result == _CLEAN_CODE


@pytest.mark.unit
def test_strip_thinking_traces_handles_multiline_think_block() -> None:
    content = "<think>\nline one\nline two\nline three\n</think>\nActual answer."
    result = _strip_thinking_traces(content)
    assert result.strip() == "Actual answer."


@pytest.mark.unit
def test_strip_thinking_traces_handles_multiple_blocks() -> None:
    content = "<think>first</think>\ntext\n<think>second</think>\nfinal"
    result = _strip_thinking_traces(content)
    assert "<think>" not in result
    assert "text" in result
    assert "final" in result


@pytest.mark.unit
def test_extract_fenced_code_blocks_single_fence() -> None:
    content = "Some prose.\n```python\ndef foo(): pass\n```\nMore prose."
    blocks = _extract_fenced_code_blocks(content)
    assert len(blocks) == 1
    assert "def foo(): pass" in blocks[0]


@pytest.mark.unit
def test_extract_fenced_code_blocks_no_fence_returns_empty() -> None:
    blocks = _extract_fenced_code_blocks(_CLEAN_CODE)
    assert blocks == []


@pytest.mark.unit
def test_extract_fenced_code_blocks_multiple_fences() -> None:
    content = "```python\ndef foo(): pass\n```\ntext\n```python\ndef bar(): pass\n```"
    blocks = _extract_fenced_code_blocks(content)
    assert len(blocks) == 2
    assert any("foo" in b for b in blocks)
    assert any("bar" in b for b in blocks)


@pytest.mark.unit
def test_check_compiles_without_errors_fenced_valid_code() -> None:
    result = _check_compiles_without_errors(_FENCED_CODE)
    assert result is None


@pytest.mark.unit
def test_check_compiles_without_errors_fenced_invalid_code() -> None:
    invalid_fenced = "```python\ndef foo(\n```"
    result = _check_compiles_without_errors(invalid_fenced)
    assert result is not None
    assert "MALFORMED" in result


@pytest.mark.unit
def test_check_compiles_without_errors_plain_valid_code() -> None:
    result = _check_compiles_without_errors(_CLEAN_CODE)
    assert result is None


@pytest.mark.unit
def test_check_compiles_without_errors_mixed_thinking_and_fence() -> None:
    # Thinking trace stripped before this function is called; but even with it
    # present, the fence extractor finds the valid code block and succeeds.
    result = _check_compiles_without_errors(_MIXED_RESPONSE)
    assert result is None


@pytest.mark.unit
def test_quality_gate_strips_thinking_traces_before_compile_check() -> None:
    gate_input = ModelQualityGateInput(
        correlation_id=uuid4(),
        task_type="code_generation",
        llm_response_content=_RESPONSE_WITH_THINKING,
        dod_deterministic=("compiles_without_errors",),
        dod_heuristic=(),
    )
    result = delta(gate_input)
    assert result.passed is True
    assert not any("compile" in r for r in result.failure_reasons)


@pytest.mark.unit
def test_quality_gate_compile_check_with_thinking_and_fences() -> None:
    """Full B9 failure case: <think>...</think>\n```python\n...\n```\nProse."""
    gate_input = ModelQualityGateInput(
        correlation_id=uuid4(),
        task_type="code_generation",
        llm_response_content=_MIXED_RESPONSE,
        dod_deterministic=("compiles_without_errors",),
        dod_heuristic=(),
    )
    result = delta(gate_input)
    assert result.passed is True
    assert not any("compile" in r for r in result.failure_reasons)


@pytest.mark.unit
def test_quality_gate_strips_thinking_traces_before_concise_check() -> None:
    # Thinking trace adds hundreds of words; clean answer is short
    verbose_thinking = "<think>\n" + ("word " * 300) + "\n</think>\n"
    short_answer = "The answer is 42."
    gate_input = ModelQualityGateInput(
        correlation_id=uuid4(),
        task_type="code_generation",
        llm_response_content=verbose_thinking + short_answer,
        dod_deterministic=(),
        dod_heuristic=("concise",),
    )
    result = delta(gate_input)
    assert result.passed is False
    assert result.quality_score == pytest.approx(1.0)
    assert any("reject-only" in r for r in result.failure_reasons)


@pytest.mark.unit
def test_quality_gate_passes_existing_tests_is_supported_not_unsupported() -> None:
    """``passes_existing_tests`` is a recognized check, not an unsupported one.

    It must not surface a ``MALFORMED: unsupported deterministic DoD check`` error
    — it is a KNOWN check that is deliberately SKIPPED (no wired acceptance-command
    executor), not an unrecognized one.
    """
    gate_input = ModelQualityGateInput(
        correlation_id=uuid4(),
        task_type="code_generation",
        llm_response_content=_CLEAN_CODE,
        dod_deterministic=("passes_existing_tests",),
        dod_heuristic=(),
    )
    result = delta(gate_input)
    assert not any("unsupported deterministic" in r for r in result.failure_reasons), (
        f"passes_existing_tests produced unsupported error: {result.failure_reasons}"
    )


@pytest.mark.unit
def test_quality_gate_passes_existing_tests_is_skipped_never_phantom_pass() -> None:
    """OMN-13850: an UNEVALUATED ``passes_existing_tests`` must NOT phantom-pass.

    Before OMN-13850 this check was aliased to ``_check_response_non_empty``, so a
    clean-but-non-empty answer whose ONLY declared deterministic check was
    ``passes_existing_tests`` returned ``passed=True`` while executing zero tests.
    Now the check is recorded as SKIPPED (no wired executor), confers NO acceptance
    authority, and the gate falls through to the no-authority verdict — a clean
    answer no longer passes on a check that evaluated nothing.
    """
    gate_input = ModelQualityGateInput(
        correlation_id=uuid4(),
        task_type="code_generation",
        llm_response_content=_CLEAN_CODE,
        dod_deterministic=("passes_existing_tests",),
        dod_heuristic=(),
    )
    result = delta(gate_input)
    assert result.passed is False
    assert result.fail_category == "fail_heuristic"
    # No phantom "passed" evidence: the skipped check produced neither a pass nor a
    # failure, so there is no deterministic-acceptance authority to promote it.
    assert result.actual_score is None


@pytest.mark.unit
def test_quality_gate_code_generation_with_thinking_passes_all_checks() -> None:
    """Full code_generation DoD with thinking trace — mirrors the B9 failure case."""
    gate_input = ModelQualityGateInput(
        correlation_id=uuid4(),
        task_type="code_generation",
        llm_response_content=_RESPONSE_WITH_THINKING,
        dod_deterministic=("compiles_without_errors", "passes_existing_tests"),
        dod_heuristic=("no_refusal", "follows_codebase_conventions"),
    )
    result = delta(gate_input)
    assert not any("compile" in r for r in result.failure_reasons)
    assert not any("unsupported" in r for r in result.failure_reasons)
