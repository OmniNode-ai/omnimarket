# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit coverage for typed inference protocol request-shaping config."""

from __future__ import annotations

from omnimarket.inference.protocol_config import (
    apply_inference_protocol,
    apply_inference_protocol_directives,
    load_inference_protocol_config,
)


def test_default_protocol_config_loads_and_suppresses_qwen_test_reasoning() -> None:
    config = load_inference_protocol_config()

    system_prompt, prompt = apply_inference_protocol_directives(
        system_prompt="You are a test generation assistant.",
        prompt="Write pytest unit tests for normalize_status.",
        model="Qwen3-Coder-30B",
        task_type="test",
        config=config,
    )

    assert system_prompt == "You are a test generation assistant."
    assert prompt.startswith("/no_think\n")
    assert prompt.endswith("Write pytest unit tests for normalize_status.")


def test_default_protocol_config_does_not_rewrite_non_qwen_model() -> None:
    _, prompt = apply_inference_protocol_directives(
        system_prompt="You are a test generation assistant.",
        prompt="Write pytest unit tests.",
        model="claude-sonnet-4-6",
        task_type="test",
    )

    assert prompt == "Write pytest unit tests."


def test_default_protocol_config_adds_qwen_provider_non_thinking_options() -> None:
    system_prompt, prompt, request_options = apply_inference_protocol(
        system_prompt="You are a production-quality code generation assistant.",
        prompt="Create a hello world effect node.",
        model="Qwen3.6-35B-A3B",
        task_type="code_generation",
        backend_id="local-coder",
    )

    assert system_prompt == "You are a production-quality code generation assistant."
    assert prompt.startswith("/no_think\n")
    assert request_options == {"chat_template_kwargs": {"enable_thinking": False}}


def test_default_protocol_config_suppresses_qwen_summarization_reasoning() -> None:
    system_prompt, prompt, request_options = apply_inference_protocol(
        system_prompt="You are a summarization assistant.",
        prompt="Summarize the verified evidence.",
        model="Qwen3.6-35B-A3B",
        task_type="summarization",
        backend_id="local-coder",
    )

    assert system_prompt == "You are a summarization assistant."
    assert prompt.startswith("/no_think\n")
    assert request_options == {"chat_template_kwargs": {"enable_thinking": False}}
