# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Tests for the Prompt Builder compute handler."""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_hostile_reviewer.handlers.handler_prompt_builder import (
    ModelPromptBuilderInput,
    ModelPromptBuilderOutput,
    build_prompt,
)


def test_short_content_no_truncation():
    result = build_prompt(
        ModelPromptBuilderInput(
            prompt_template_id="adversarial_reviewer_pr",
            context_content="small diff content",
            model_context_window=128_000,
        )
    )
    assert isinstance(result, ModelPromptBuilderOutput)
    assert "adversarial" in result.system_prompt.lower()
    assert "small diff content" in result.user_prompt
    assert result.truncated is False


def test_large_content_truncated_head_tail():
    big = "A" * 500_000
    result = build_prompt(
        ModelPromptBuilderInput(
            prompt_template_id="adversarial_reviewer_pr",
            context_content=big,
            model_context_window=8_000,  # small window forces truncation
        )
    )
    assert len(result.user_prompt) < len(big)
    assert "[truncated" in result.user_prompt
    assert result.truncated is True
    assert result.original_content_chars == 500_000
    assert result.truncated_content_chars < result.original_content_chars


def test_plan_template():
    result = build_prompt(
        ModelPromptBuilderInput(
            prompt_template_id="adversarial_reviewer_plan",
            context_content="# My Plan\n\nDo the thing.",
            model_context_window=128_000,
        )
    )
    assert (
        "plan" in result.user_prompt.lower() or "plan" in result.system_prompt.lower()
    )


def test_unknown_template_raises():
    with pytest.raises(ValueError, match="Unknown prompt_template_id"):
        build_prompt(
            ModelPromptBuilderInput(
                prompt_template_id="nonexistent",
                context_content="content",
                model_context_window=128_000,
            )
        )


def test_persona_markdown_prepended():
    result = build_prompt(
        ModelPromptBuilderInput(
            prompt_template_id="adversarial_reviewer_pr",
            context_content="some diff",
            model_context_window=128_000,
            persona_markdown="You are extra strict.",
        )
    )
    assert result.system_prompt.startswith("You are extra strict.")


def test_context_window_minimum_enforced():
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="greater than or equal to 1024"):
        ModelPromptBuilderInput(
            prompt_template_id="adversarial_reviewer_pr",
            context_content="content",
            model_context_window=100,  # below ge=1024
        )
