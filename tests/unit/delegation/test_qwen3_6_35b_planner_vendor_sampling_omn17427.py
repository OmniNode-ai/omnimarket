# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-17427: the .200 planner's Qwen3.6-35B-A3B gets the vendor's non-thinking request.

The Mac Studio planner rung (backend ``local-studio-planner``) moved from gpt-oss-120b
to Qwen3.6-35B-A3B, served as ``qwen3.6-35b-a3b``. The model card's non-thinking mode
is ``temperature=0.7, top_p=0.80, top_k=20, min_p=0.0, presence_penalty=1.5`` with
``enable_thinking: false``. These tests drive the committed config, with a control on
the Qwen3.8-27B rungs, which must keep the request they were measured with.
"""

from __future__ import annotations

import pytest

from omnimarket.inference.protocol_config import (
    apply_inference_protocol,
    load_inference_protocol_config,
    resolve_inference_protocol_default_temperature,
)

pytestmark = pytest.mark.unit

_PLANNER_MODEL = "qwen3.6-35b-a3b"
_RUNG_CLASSES = (
    "review",
    "reasoning",
    "complex_reasoning",
    "planning",
    "research",
    "escalation",
)


@pytest.mark.parametrize("task_type", _RUNG_CLASSES)
def test_every_class_the_rung_serves_gets_the_vendor_non_thinking_request(
    task_type: str,
) -> None:
    config = load_inference_protocol_config()
    _, _, options = apply_inference_protocol(
        system_prompt="You are a planner.",
        prompt="Plan the change.",
        model=_PLANNER_MODEL,
        task_type=task_type,
        config=config,
    )
    assert options["top_p"] == 0.8
    assert options["top_k"] == 20
    assert options["min_p"] == 0.0
    assert options["presence_penalty"] == 1.5
    assert options["chat_template_kwargs"] == {"enable_thinking": False}
    assert "temperature" not in options
    assert (
        resolve_inference_protocol_default_temperature(
            model=_PLANNER_MODEL, task_type=task_type, config=config
        )
        == 0.7
    )


def test_a_class_outside_the_qwen_lists_still_runs_non_thinking() -> None:
    config = load_inference_protocol_config()
    _, _, options = apply_inference_protocol(
        system_prompt="",
        prompt="Classify this.",
        model=_PLANNER_MODEL,
        task_type="classification",
        config=config,
    )
    assert options["chat_template_kwargs"] == {"enable_thinking": False}
    assert options["top_k"] == 20


def test_control_the_qwen3_8_rungs_keep_their_measured_request() -> None:
    config = load_inference_protocol_config()
    _, _, options = apply_inference_protocol(
        system_prompt="You are a planner.",
        prompt="Plan the change.",
        model="Qwen3.8-27B",
        task_type="planning",
        config=config,
    )
    assert options == {"chat_template_kwargs": {"enable_thinking": False}}
    assert (
        resolve_inference_protocol_default_temperature(
            model="Qwen3.8-27B", task_type="planning", config=config
        )
        is None
    )
