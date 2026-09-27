# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""GLM delegation calls suppress hidden reasoning through the packaged profile."""

import pytest

from omnimarket.inference.protocol_config import (
    apply_inference_protocol,
    load_inference_protocol_config,
)

pytestmark = pytest.mark.unit

GLM_TASK_TYPES = (
    "code_generation",
    "code_review",
    "refactor",
    "reasoning",
    "complex_reasoning",
    "research",
    "document",
    "documentation",
    "summarization",
    "review",
    "planning",
    "test",
)


def test_glm_no_think_profile_is_enabled() -> None:
    config = load_inference_protocol_config()

    profile = next(
        profile
        for profile in config.profiles
        if profile.profile_id == "cloud-glm-no-think"
    )
    assert profile.enabled


@pytest.mark.parametrize("task_type", GLM_TASK_TYPES)
def test_glm_task_class_disables_thinking(task_type: str) -> None:
    # The workflow supplies the task type, but omits backend_id.
    _, _, request_options = apply_inference_protocol(
        system_prompt="You are a helpful assistant.",
        prompt="Return the requested deliverable.",
        model="glm-5.3-flash",
        task_type=task_type,
        config=load_inference_protocol_config(),
    )

    assert request_options["thinking"] == {"type": "disabled"}


@pytest.mark.parametrize("model", ["glm-5.1", "zai/glm-5.1"])
def test_glm_model_name_variants_disable_thinking(model: str) -> None:
    _, _, request_options = apply_inference_protocol(
        system_prompt="You are a helpful assistant.",
        prompt="Return the requested deliverable.",
        model=model,
        task_type="summarization",
        config=load_inference_protocol_config(),
    )

    assert request_options["thinking"] == {"type": "disabled"}


@pytest.mark.parametrize("model", ["qwen3.8-27b", "gemini-2.5-flash"])
def test_other_model_families_do_not_get_glm_thinking_option(model: str) -> None:
    _, _, request_options = apply_inference_protocol(
        system_prompt="You are a helpful assistant.",
        prompt="Return the requested deliverable.",
        model=model,
        task_type="code_generation",
        config=load_inference_protocol_config(),
    )

    assert "thinking" not in request_options
