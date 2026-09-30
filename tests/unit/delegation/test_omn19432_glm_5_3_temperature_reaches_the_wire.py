# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19432: the glm-5.3 profile's temperature reaches the wire instead of raising.

The ``cloud-glm-5-3-thinking`` profile (omnimarket#3114) carried ``temperature: 1.0`` in
its ``request_options``. ``temperature`` is a reserved wire key on both delegation paths
(``LocalDelegationDispatchPort`` and ``HandlerInferenceIntent`` each refuse a profile that
writes it, one producer per wire key, OMN-15482), so the first glm-5.3 call raised
``ValueError: provider request options cannot override: temperature`` before any request
left the process. The profile tests passed because they only read the merged options.

The fix keeps one producer per wire key. A profile declares a ``default_temperature``, a
first-class field and not a request option, and the dispatch path resolves the outbound
temperature as: the caller's value, else the matching profile's default, else the task
class default it always used. ``top_p`` is not reserved and stays a request option.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from omnimarket.inference.protocol_config import (
    apply_inference_protocol,
    load_inference_protocol_config,
    resolve_inference_protocol_default_temperature,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.handler_delegate_skill import (
    HandlerDelegateSkill,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.models.model_delegate_skill_request import (
    ModelDelegateSkillRequest,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_local_delegation_dispatch import (
    LocalDelegationDispatchPort,
)
from omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow import (
    _resolve_call_temperature,
)
from omnimarket.nodes.node_llm_delegation_call_effect import (
    ModelLlmDelegationCallRequest,
    ModelLlmDelegationCallResult,
)
from omnimarket.routing import delegation_backend_resolution

pytestmark = pytest.mark.unit

_ANSWER = '{"decision": "keep", "rationale": "The tree is dirty."}'
_CONTRACT: dict[str, Any] = {
    "type": "object",
    "required": ["decision", "rationale"],
    "properties": {"decision": {"type": "string"}, "rationale": {"type": "string"}},
}


def _backends(model: str) -> list[dict[str, Any]]:
    return [
        {
            "backend_id": "glm-under-test",
            "endpoint_url": "https://api.z.ai/api/coding/paas/v4/chat/completions",
            "model_name": model,
            "tier": "cheap_cloud",
            "max_tokens": 16384,
            "timeout_ms": 90000,
            "capabilities": ["reasoning"],
        }
    ]


class _RecordingEffect:
    def __init__(self) -> None:
        self.calls: list[ModelLlmDelegationCallRequest] = []

    def __call__(
        self, request: ModelLlmDelegationCallRequest
    ) -> ModelLlmDelegationCallResult:
        self.calls.append(request)
        return ModelLlmDelegationCallResult(
            request_id=request.request_id, success=True, content=_ANSWER
        )


async def _dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    model: str,
    temperature: float | None,
) -> ModelLlmDelegationCallRequest:
    monkeypatch.setattr(
        delegation_backend_resolution,
        "load_bifrost_backends",
        lambda **_: _backends(model),
    )
    effect = _RecordingEffect()
    port = LocalDelegationDispatchPort(
        effect_handler=effect,
        evidence_db_path=tmp_path / f"{uuid4()}.sqlite",
        effect_process_boundary=False,
    )
    response = await HandlerDelegateSkill(dispatch_port=port).handle(
        ModelDelegateSkillRequest(
            prompt="Classify this worktree. Answer only JSON.",
            task_type="review",
            source="external-client",
            backend_id="glm-under-test",
            response_contract=_CONTRACT,
            **({} if temperature is None else {"temperature": temperature}),
        )
    )
    assert response.status == "completed", response
    assert len(effect.calls) == 1
    return effect.calls[0]


def test_the_profile_declares_its_temperature_as_a_field_not_a_request_option() -> None:
    config = load_inference_protocol_config()
    _, _, options = apply_inference_protocol(
        system_prompt="s",
        prompt="p",
        model="glm-5.3",
        task_type="review",
        config=config,
    )
    assert "temperature" not in options
    assert options["thinking"] == {"type": "enabled"}
    assert options["top_p"] == 0.95
    assert (
        resolve_inference_protocol_default_temperature(
            model="glm-5.3", task_type="review", config=config
        )
        == 1.0
    )


@pytest.mark.parametrize(
    "model", ["glm-5.2", "glm-5.3-flashx", "qwen3.8-27b", "gemini-2.5-flash"]
)
def test_no_other_model_gets_a_profile_temperature(model: str) -> None:
    # glm-5.3 and glm-5.3-flash carry the documented temperature (OMN-19432); every
    # other model keeps the effect default.
    assert (
        resolve_inference_protocol_default_temperature(
            model=model, task_type="review", config=load_inference_protocol_config()
        )
        is None
    )


async def test_the_port_sends_the_profile_temperature_when_the_caller_sent_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    call = await _dispatch(tmp_path, monkeypatch, model="glm-5.3", temperature=None)

    assert call.temperature == 1.0
    assert call.provider_request_options == {
        "thinking": {"type": "enabled"},
        "top_p": 0.95,
    }


async def test_a_caller_temperature_still_wins_over_the_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    call = await _dispatch(tmp_path, monkeypatch, model="glm-5.3", temperature=0.2)

    assert call.temperature == 0.2


async def test_flash_sends_the_documented_temperature_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    call = await _dispatch(
        tmp_path, monkeypatch, model="glm-5.3-flash", temperature=None
    )

    assert call.temperature == 1.0
    assert call.provider_request_options == {
        "thinking": {"type": "enabled"},
        "top_p": 0.95,
    }


async def test_a_model_without_a_profile_temperature_keeps_the_effect_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    call = await _dispatch(tmp_path, monkeypatch, model="glm-5.2", temperature=None)

    assert (
        call.temperature
        == ModelLlmDelegationCallRequest.model_fields["temperature"].default
    )


def test_the_bus_path_resolves_the_same_precedence() -> None:
    assert (
        _resolve_call_temperature(
            request_temperature=None, model="glm-5.3", task_type="review"
        )
        == 1.0
    )
    assert (
        _resolve_call_temperature(
            request_temperature=0.4, model="glm-5.3", task_type="review"
        )
        == 0.4
    )
    assert (
        _resolve_call_temperature(
            request_temperature=None, model="glm-5.3-flash", task_type="review"
        )
        == 1.0
    )
    assert (
        _resolve_call_temperature(
            request_temperature=None, model="glm-5.2", task_type="review"
        )
        == 0.3
    )
