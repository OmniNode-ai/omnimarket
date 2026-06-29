# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 OmniNode Team
"""Contract guard for the OMN-13335 Gemini-ceiling thinking-suppression fix.

OMN-13335 (the SEA up-tier discriminator) requires that an escalating
``code_generation`` delegation can reach a CLOUD ceiling terminal that PASSES
the 0.85 quality bar. The live failure (CID ffa90b77) was NOT a weak ceiling
model and NOT a miscalibrated bar — it was TRUNCATION:

  * the ceiling routes to ``gemini-2.5-flash`` (routing_tiers ``claude`` slot ->
    bifrost backend ``cloud-gemini-pro``), a THINKING model whose reasoning
    tokens are drawn from the SAME completion budget as the visible answer on
    the AI Studio OpenAI-compatibility endpoint;
  * with thinking ON, the budget is spent on hidden reasoning and the visible
    code completion is truncated (live: tokens_output=18, judge adequacy 0.200,
    combined terminal score 0.680 < required_bar 0.85);
  * a live dev-lane probe proved the model is fully capable when thinking is
    OFF (``reasoning_effort: none`` -> finish_reason=stop with a complete
    function vs finish_reason=length with 8-9 tokens on the default/`low`
    reasoning_effort at the same small max_tokens).

The fix is a contract-only inference-protocol profile
(``cloud-gemini-flash-no-think-code``) that injects ``reasoning_effort: none``
for the Gemini ceiling on the code task classes, mirroring the existing
``local-qwen-no-think`` thinking-off pattern. It does NOT touch the 0.85 bar or
the quality gate. This guard pins that the profile resolves the thinking-off
request option for the discriminator's exact (model, task_type) so a config edit
cannot silently re-introduce the truncation regression.

Related:
    - OMN-13335: escalation up-tier discriminator (this guard)
    - OMN-13351: ceiling repointed to cloud-gemini-pro (gemini-2.5-flash)
    - OMN-13345: backend max_tokens threaded onto the routing decision
    - OMN-12813: local-qwen thinking-off precedent in inference_protocols.v1
"""

from __future__ import annotations

import pytest

from omnimarket.inference.protocol_config import (
    apply_inference_protocol,
    load_inference_protocol_config,
)

# The exact discriminator coordinates: the code_generation escalation ceiling
# model and task class.
_CEILING_MODEL = "gemini-2.5-flash"
_CODE_TASK_TYPE = "code_generation"


@pytest.mark.unit
def test_gemini_ceiling_resolves_reasoning_effort_none_for_code() -> None:
    """The Gemini ceiling code path must resolve ``reasoning_effort: none``.

    Without thinking-off the gemini-2.5-flash thinking tokens consume the
    completion budget and the visible code answer is truncated below the 0.85
    code_generation bar — the exact OMN-13335 failure mode. This asserts the
    contract-resolved request options carry the thinking-off directive for the
    discriminator's (model, task_type).
    """
    _system_prompt, _prompt, request_options = apply_inference_protocol(
        system_prompt="You are a code generation assistant.",
        prompt="Implement is_palindrome(s: str) -> bool.",
        model=_CEILING_MODEL,
        task_type=_CODE_TASK_TYPE,
        backend_id="cloud-gemini-pro",
    )
    assert request_options.get("reasoning_effort") == "none", (
        "gemini-2.5-flash code_generation must resolve reasoning_effort=none so "
        "thinking tokens do not truncate the visible code completion below the "
        f"0.85 bar; resolved request_options={request_options!r}"
    )


@pytest.mark.unit
def test_gemini_ceiling_profile_present_and_enabled() -> None:
    """The cloud Gemini thinking-off profile must exist, be enabled, and target code.

    Pins the profile identity so a config refactor cannot drop the discriminator's
    thinking-off coverage without this guard failing loud.
    """
    config = load_inference_protocol_config()
    profile = next(
        (
            p
            for p in config.profiles
            if p.profile_id == "cloud-gemini-flash-no-think-code"
        ),
        None,
    )
    assert profile is not None, (
        "inference_protocols.v1.yaml must declare the "
        "'cloud-gemini-flash-no-think-code' profile for the OMN-13335 ceiling fix"
    )
    assert profile.enabled, "the Gemini ceiling thinking-off profile must be enabled"
    assert _CODE_TASK_TYPE in profile.task_types, (
        "the Gemini ceiling thinking-off profile must cover code_generation"
    )
    assert profile.request_options.get("reasoning_effort") == "none", (
        "the Gemini ceiling thinking-off profile must inject reasoning_effort=none"
    )
    assert any(
        "gemini-2.5-flash" in pattern.lower() for pattern in profile.model_name_patterns
    ), "the profile must match the gemini-2.5-flash ceiling model"


@pytest.mark.unit
def test_gemini_thinking_off_not_applied_to_non_code_task() -> None:
    """Thinking-off must be scoped to the code path, not prose tasks.

    A research/document prose task on gemini benefits from reasoning, so the
    code-scoped thinking-off profile must NOT fire for it. Scoping prevents the
    fix from degrading the prose escalation classes.
    """
    _system_prompt, _prompt, request_options = apply_inference_protocol(
        system_prompt="You are a code research assistant.",
        prompt="Explain the time complexity of mergesort.",
        model=_CEILING_MODEL,
        task_type="research",
        backend_id="cloud-gemini-pro",
    )
    assert "reasoning_effort" not in request_options, (
        "the code-scoped Gemini thinking-off profile must not apply to research; "
        f"resolved request_options={request_options!r}"
    )


@pytest.mark.unit
def test_gemini_ceiling_injects_final_artifact_only_directive() -> None:
    """The code tier must receive a final-artifact-only system directive.

    The OMN-13335 discriminator residual: at the gemini-2.5-flash code ceiling
    the answer is correct (judge_score=1.0) but the deterministic
    ``final_artifact_only`` DoD vetoes it because the model wraps its code in
    prose OUTSIDE the fenced block (``_check_final_artifact_only`` fails on ANY
    non-whitespace text outside the fence, before OR after). The fix is a
    prompt-level instruction — NOT a gate change — telling the model to emit
    only the fenced artifact so it legitimately satisfies the DoD. This pins
    that the code-scoped profile injects that instruction into the outbound
    system prompt for the discriminator's (model, task_type).
    """
    base_system_prompt = "You are a code generation assistant."
    next_system_prompt, _prompt, _request_options = apply_inference_protocol(
        system_prompt=base_system_prompt,
        prompt="Implement is_palindrome(s: str) -> bool.",
        model=_CEILING_MODEL,
        task_type=_CODE_TASK_TYPE,
        backend_id="cloud-gemini-pro",
    )
    appended = next_system_prompt[len(base_system_prompt) :].lower()
    assert "final code artifact only" in appended, (
        "the code ceiling system prompt must instruct final-artifact-only output "
        f"so the model satisfies the final_artifact_only DoD; got={appended!r}"
    )
    assert "before or after" in appended, (
        "the directive must forbid prose on BOTH sides of the fence (the "
        "final_artifact_only check rejects any non-whitespace text outside the "
        f"code block, before or after); got={appended!r}"
    )


@pytest.mark.unit
def test_final_artifact_only_directive_not_applied_to_non_code_task() -> None:
    """The final-artifact-only directive must stay scoped to the code path.

    A prose task must NOT receive the artifact-only instruction, otherwise the
    prose escalation classes would be told to emit only fenced code. Scoping
    keeps the OMN-13335 fix from perturbing other task classes.
    """
    base_system_prompt = "You are a code research assistant."
    next_system_prompt, _prompt, _request_options = apply_inference_protocol(
        system_prompt=base_system_prompt,
        prompt="Explain the time complexity of mergesort.",
        model=_CEILING_MODEL,
        task_type="research",
        backend_id="cloud-gemini-pro",
    )
    assert "final code artifact only" not in next_system_prompt.lower(), (
        "the final-artifact-only directive must not be injected for prose tasks; "
        f"got system_prompt={next_system_prompt!r}"
    )
