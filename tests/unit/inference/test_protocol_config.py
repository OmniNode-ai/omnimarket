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


def test_default_protocol_config_does_not_no_think_qwen_summarization_task_type() -> (
    None
):
    """OMN-14626 regression: local-qwen-no-think must NOT match task types
    outside its declared ``task_types`` allowlist.

    Before OMN-14626, ``local-qwen-no-think`` declared no ``task_types`` at
    all, and ``_profile_matches`` only consults ``system_prompt_contains``
    when ``task_type is None``. The practical effect was that ANY non-None
    ``task_type`` value — including ``summarization``, which the profile was
    never meant to cover (none of its ``system_prompt_contains`` phrases are
    summarization-related) — matched the profile unconditionally, regardless
    of the system prompt's content. This test proves that latent over-broad
    match is closed: RED (this exact call returned the no-think directive)
    before the OMN-14626 ``task_types`` allowlist was added to
    ``inference_protocols.v1.yaml``; GREEN (no directive applied) after.
    """
    system_prompt, prompt, request_options = apply_inference_protocol(
        system_prompt="You are a summarization assistant.",
        prompt="Summarize the verified evidence.",
        model="Qwen3.6-35B-A3B",
        task_type="summarization",
        backend_id="local-coder",
    )

    assert system_prompt == "You are a summarization assistant."
    assert prompt == "Summarize the verified evidence."
    assert request_options == {}


def test_qwen_no_think_task_types_gate_code_generation_independent_of_prompt() -> None:
    """OMN-14626: task_types makes ``local-qwen-no-think`` authoritative for
    ``code_generation`` (and the other 3 code-writing task types) regardless
    of the system prompt's wording.

    This is the delegation-unblock contract this ticket exists to guarantee:
    a code_generation call must get ``enable_thinking: false`` even when the
    system prompt does not contain one of the profile's legacy
    ``system_prompt_contains`` phrases ("test generation", "test engineer",
    "code generation", "production-quality code"). Note: on the current
    matcher, a provided non-None ``task_type`` already bypassed
    ``system_prompt_contains`` before this ticket too (see the sibling
    summarization test above for the actual RED->GREEN delta OMN-14626
    produced) — this test pins the INTENDED, now-explicit contract so a
    future edit that narrows ``system_prompt_contains`` or otherwise changes
    the matcher cannot silently regress code_generation specifically.
    """
    system_prompt, prompt, request_options = apply_inference_protocol(
        system_prompt="You are a helpful assistant.",
        prompt="Write a fibonacci function.",
        model="Qwen3.6-35B-A3B",
        task_type="code_generation",
        backend_id="local-coder",
    )

    assert system_prompt == "You are a helpful assistant."
    assert prompt.startswith("/no_think\n")
    assert request_options == {"chat_template_kwargs": {"enable_thinking": False}}


def test_qwen_no_think_task_types_gate_agent_delegation_independent_of_prompt() -> None:
    """OMN-15187: ``local-qwen-no-think`` must also be authoritative for
    ``agent_delegation``.

    Before OMN-15187, ``agent_delegation`` was absent from the profile's
    ``task_types`` allowlist added by OMN-14626, so a non-None
    ``task_type="agent_delegation"`` call short-circuited ``_profile_matches``
    to ``task_type in profile.task_types`` -> ``False`` and never reached
    ``enable_thinking: false``, leaving the local Qwen3.6 backend in its
    thinking-ON default. Live-proven (OMN-15187): with thinking left on, the
    model burns its whole per-tick ``max_tokens`` budget (96-600, per
    OMN-15170's driver) on hidden chain-of-thought and returns empty
    ``content`` — the same failure class OMN-14626 fixed for
    code_generation/test/validator_generation/refactor, now closed for
    agent_delegation too. This test proves the fix: RED (no directive
    applied, empty ``request_options``) before ``agent_delegation`` was added
    to the YAML allowlist; GREEN (``/no_think`` prefix + ``enable_thinking:
    false``) after.
    """
    system_prompt, prompt, request_options = apply_inference_protocol(
        system_prompt="You are a tactical decision assistant.",
        prompt="Decide the next battle action given the current game state.",
        model="Qwen3.6-35B-A3B",
        task_type="agent_delegation",
        backend_id="local-coder-mlx",
    )

    assert system_prompt == "You are a tactical decision assistant."
    assert prompt.startswith("/no_think\n")
    assert request_options == {"chat_template_kwargs": {"enable_thinking": False}}


def test_qwen_no_think_task_types_allowlist_unchanged_members_still_match() -> None:
    """OMN-15187 regression: adding ``agent_delegation`` to
    ``local-qwen-no-think``'s ``task_types`` allowlist must not disturb the
    four members OMN-14626 already declared there.

    Pins both the exact allowlist contents on the loaded config (guards
    against an accidental drop/rename/reorder in the YAML edit) and that each
    pre-existing task_type still independently triggers the no-think
    directive end-to-end through ``apply_inference_protocol``.
    """
    config = load_inference_protocol_config()
    profile = next(p for p in config.profiles if p.profile_id == "local-qwen-no-think")

    assert profile.task_types == (
        "code_generation",
        "test",
        "validator_generation",
        "refactor",
        "agent_delegation",
    )

    for task_type in ("code_generation", "test", "validator_generation", "refactor"):
        _, prompt, request_options = apply_inference_protocol(
            system_prompt="You are a helpful assistant.",
            prompt=f"Do the {task_type} task.",
            model="Qwen3.6-35B-A3B",
            task_type=task_type,
            backend_id="local-coder",
            config=config,
        )
        assert prompt.startswith("/no_think\n"), task_type
        assert request_options == {
            "chat_template_kwargs": {"enable_thinking": False}
        }, task_type
