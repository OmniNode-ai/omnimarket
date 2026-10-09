# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-20469: task type ``reasoning`` runs on Qwen3.8-27B with thinking on.

The adoption rests on a pre-registered 60-item replay judged blind by two
judges: thinking on was accepted 48 of 60 times; thinking off, as shipped, was
accepted 28 of 60 (Codex judge) and 35 of 60 (Opus judge). The gains, 33.3 and
21.7 points with exact paired p of 0.00001 and 0.00098, clear the bar of 15
points and p below 0.05 on each judge. Both arms ran at temperature 0.3 and the
same ``max_tokens``, so this change moves the ``chat_template_kwargs`` switch
and the ``/no_think`` prefix for ``reasoning`` and nothing else.

These tests pin the outbound request. They are written against the egress the
provider receives, not against the YAML, so they fail on the unchanged profile
(RED) and keep every other task class where it was.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Final
from uuid import uuid4

import pytest
from omnibase_core.models.delegation.wire import ModelInferenceIntent
from pydantic import ValidationError

from omnimarket.inference.protocol_config import (
    ModelInferenceProtocolConfig,
    ModelInferenceProtocolProfile,
    apply_inference_protocol,
)
from omnimarket.models.delegation.wire.model_routing_decision import (
    ModelRoutingDecision,
)
from omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow import (
    HandlerDelegationWorkflow,
    _resolve_call_temperature,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_request import (
    ModelDelegationRequest,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_inference_intent import (
    _build_messages_and_request_options,
    _merge_provider_request_options,
)

LOCAL_MODEL: Final[str] = "Qwen3.8-27B"
STUDIO_PLANNER_ALIAS: Final[str] = "qwen3.6-35b-a3b"
SYSTEM_PROMPT: Final[str] = "You are a helpful assistant."
USER_PROMPT: Final[str] = "Explain why a retry loop needs a total deadline."

# The temperature both arms of the 60-item replay ran at.
CONFIRMED_TEMPERATURE: Final[float] = 0.3
ROUTED_MAX_TOKENS: Final[int] = 512


def _resolve(
    task_type: str, model: str = LOCAL_MODEL
) -> tuple[str, str, dict[str, Any]]:
    return apply_inference_protocol(
        system_prompt=SYSTEM_PROMPT,
        prompt=USER_PROMPT,
        model=model,
        task_type=task_type,
    )


@pytest.mark.unit
def test_reasoning_sends_thinking_on_with_no_no_think_prefix() -> None:
    system_prompt, prompt, options = _resolve("reasoning")

    assert options.get("chat_template_kwargs") == {"enable_thinking": True}, options
    assert "/no_think" not in prompt
    assert "/no_think" not in system_prompt
    assert prompt == USER_PROMPT
    assert system_prompt == SYSTEM_PROMPT


@pytest.mark.unit
def test_reasoning_temperature_is_unchanged_at_the_confirmed_value() -> None:
    """No profile prescribes a temperature for reasoning; the 0.3 default holds."""

    assert (
        _resolve_call_temperature(
            request_temperature=None, model=LOCAL_MODEL, task_type="reasoning"
        )
        == CONFIRMED_TEMPERATURE
    )


@pytest.mark.unit
def test_complex_reasoning_stays_thinking_off() -> None:
    """Only `reasoning` was confirmed; `complex_reasoning` keeps its measured profile."""

    _, prompt, options = _resolve("complex_reasoning")

    assert options.get("chat_template_kwargs") == {"enable_thinking": False}, options
    assert prompt == f"/no_think\n{USER_PROMPT}"


@pytest.mark.unit
@pytest.mark.parametrize(
    "task_type",
    [
        "document",
        "documentation",
        "summarization",
        "research",
        "review",
        "code_review",
        "planning",
        "escalation",
        "complex_reasoning",
    ],
)
def test_the_other_prose_classes_stay_thinking_off(task_type: str) -> None:
    _, prompt, options = _resolve(task_type)

    assert options.get("chat_template_kwargs") == {"enable_thinking": False}, options
    assert prompt == f"/no_think\n{USER_PROMPT}"


@pytest.mark.unit
def test_reasoning_on_the_studio_planner_alias_stays_thinking_off() -> None:
    """The thinking-on result was measured on Qwen3.8-27B only.

    Qwen3.6-35B-A3B on the Studio planner rung keeps the switch its own vendor
    profile sets, because nothing here measured it with thinking on.
    """

    _, _, options = _resolve("reasoning", model=STUDIO_PLANNER_ALIAS)

    assert options["chat_template_kwargs"] == {"enable_thinking": False}, options


@pytest.mark.unit
def test_reasoning_on_a_glm_model_is_not_given_the_qwen_switch() -> None:
    _, prompt, options = _resolve("reasoning", model="glm-5.3-flash")

    assert "chat_template_kwargs" not in options
    assert "/no_think" not in prompt


@pytest.mark.unit
@pytest.mark.usefixtures("stub_provider_quota_reader")
def test_reasoning_payload_at_the_provider_boundary_carries_thinking_on() -> None:
    """Profile matching alone does not prove the egress.

    Run the orchestrator and the inference effect's request builders with a
    wire round-trip between them, and assert on the outbound provider payload:
    thinking on, no `/no_think` anywhere, temperature 0.3 and the routed
    `max_tokens` unchanged.
    """

    workflow = HandlerDelegationWorkflow(workflows={})
    correlation_id = uuid4()
    request = ModelDelegationRequest(
        prompt=USER_PROMPT,
        system_prompt=SYSTEM_PROMPT,
        task_type="reasoning",
        correlation_id=correlation_id,
        emitted_at=datetime.now(UTC),
    )
    assert workflow.handle_delegation_request(request)
    decision = ModelRoutingDecision(
        correlation_id=correlation_id,
        task_type="reasoning",
        selected_model=LOCAL_MODEL,
        selected_backend_id=uuid4(),
        endpoint_url="http://test-local-reasoning:8000/v1/chat/completions",
        cost_tier="local",
        max_context_tokens=32768,
        max_tokens=ROUTED_MAX_TOKENS,
        system_prompt=SYSTEM_PROMPT,
        rationale="Local reasoning provider-boundary regression.",
        tier_name="local",
        route="local-heavy-reasoning",
        provider="local",
    )
    intents = workflow.handle_routing_decision(decision)
    assert len(intents) == 1
    assert isinstance(intents[0], ModelInferenceIntent)
    intent = ModelInferenceIntent.model_validate_json(intents[0].model_dump_json())

    messages, options = _build_messages_and_request_options(intent)
    payload = _merge_provider_request_options(
        {
            "model": intent.model,
            "messages": messages,
            "max_tokens": intent.max_tokens,
            "temperature": intent.temperature,
        },
        options,
    )

    assert payload["chat_template_kwargs"] == {"enable_thinking": True}, payload
    assert all("/no_think" not in str(m["content"]) for m in payload["messages"])
    assert payload["temperature"] == CONFIRMED_TEMPERATURE
    assert payload["max_tokens"] == ROUTED_MAX_TOKENS


def _profile(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"profile_id": "options-only", "task_types": ["reasoning"]}
    base.update(overrides)
    return base


@pytest.mark.unit
def test_a_profile_may_shape_the_request_without_a_prompt_directive() -> None:
    config = ModelInferenceProtocolConfig.model_validate(
        {
            "schema_version": "inference_protocols.v1",
            "profiles": [
                _profile(request_options={"chat_template_kwargs": {"x": True}})
            ],
        }
    )

    system_prompt, prompt, options = apply_inference_protocol(
        system_prompt=SYSTEM_PROMPT,
        prompt=USER_PROMPT,
        model=LOCAL_MODEL,
        task_type="reasoning",
        config=config,
    )

    assert (system_prompt, prompt) == (SYSTEM_PROMPT, USER_PROMPT)
    assert options == {"chat_template_kwargs": {"x": True}}


@pytest.mark.unit
def test_a_profile_with_no_directive_and_no_effect_is_rejected() -> None:
    """Negative control: dropping the directive requirement must not admit a no-op profile."""

    with pytest.raises(ValidationError):
        ModelInferenceProtocolProfile.model_validate(_profile())
