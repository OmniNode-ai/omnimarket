# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A class that declares the facts-first shape sends it, on both paths (OMN-19432).

The two places a delegated task is stated to a model, the in-process port that
``onex delegate`` uses and the bus orchestrator's inference intent, compose the
user turn from the same function. These tests read the bytes each one hands
to the model: the facts lead for a declaring class, the prompt follows
unchanged, and a class that declares nothing, code_review included, gets the
prompt exactly as the caller wrote it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_DNS, UUID, uuid4, uuid5

import pytest

from omnimarket.models.delegation.wire.model_delegate_skill_request import (
    ModelDelegateSkillRequest,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.handler_delegate_skill import (
    HandlerDelegateSkill,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports import (
    port_local_delegation_dispatch as local_port_module,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_local_delegation_dispatch import (
    LocalDelegationDispatchPort,
)
from omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow import (
    HandlerDelegationWorkflow,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_request import (
    ModelDelegationRequest,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_inference_intent import (
    ModelInferenceIntent,
)
from omnimarket.nodes.node_delegation_routing_reducer.models.model_routing_decision import (
    ModelRoutingDecision,
)
from omnimarket.nodes.node_facts_first_prompt_compute.handlers.handler_facts_first_prompt import (
    FACTS_FIRST_HEADER,
    TASK_HEADER,
)
from omnimarket.nodes.node_llm_delegation_call_effect import (
    ModelLlmDelegationCallRequest,
    ModelLlmDelegationCallResult,
)
from omnimarket.routing import delegation_backend_resolution

_PROMPT = (
    "Review this change to `retry_delay` in src/pkg/retry.py. You must cite the "
    "line number of each defect. Answer in at most 100 words.\n\n"
    "```diff\n@@ -10,2 +10,3 @@ def retry_delay(attempt):\n"
    "     base = 2\n-    return base ** attempt\n+    cap = 30\n```"
)
_ANSWER = "### ANSWER\nThe cap on line 11 is a fixed 30 and ignores the caller."


def _backend_id(name: str) -> UUID:
    return uuid5(NAMESPACE_DNS, f"omninode.ai/backends/{name}")


# ---------------------------------------------------------------------------
# Bus path: the inference intent
# ---------------------------------------------------------------------------


def _intent_prompt(task_type: str, prompt: str) -> str:
    handler = HandlerDelegationWorkflow()
    correlation_id = uuid4()
    handler.handle_delegation_request(
        ModelDelegationRequest(
            prompt=prompt,
            task_type=task_type,
            correlation_id=correlation_id,
            emitted_at=datetime.now(UTC),
        )
    )
    intents = handler.handle_routing_decision(
        ModelRoutingDecision(
            correlation_id=correlation_id,
            task_type=task_type,
            selected_model="Qwen3.8-27B",
            selected_backend_id=_backend_id("local-heavy-reasoning"),
            endpoint_url="http://test-llm:8000/v1/chat/completions",
            cost_tier="low",
            max_context_tokens=8192,
            max_tokens=65536,
            system_prompt="You are an assistant.",
            rationale="test",
            dod_deterministic=("response_non_empty",),
            dod_heuristic=("no_refusal", "accurate"),
        )
    )
    inference = [i for i in intents if isinstance(i, ModelInferenceIntent)]
    assert len(inference) == 1, intents
    return inference[0].prompt


@pytest.mark.unit
@pytest.mark.parametrize("task_type", ["review", "document", "research"])
def test_bus_intent_for_a_declaring_class_leads_with_the_facts(task_type: str) -> None:
    sent = _intent_prompt(task_type, _PROMPT)

    assert FACTS_FIRST_HEADER in sent
    assert f"{TASK_HEADER}\n{_PROMPT}" in sent
    assert sent.index(FACTS_FIRST_HEADER) < sent.index(_PROMPT)
    assert "1. You must cite the line number of each defect." in sent


@pytest.mark.unit
@pytest.mark.parametrize("task_type", ["code_review", "test", "planning"])
def test_bus_intent_for_any_other_class_carries_the_prompt_as_written(
    task_type: str,
) -> None:
    sent = _intent_prompt(task_type, _PROMPT)

    assert FACTS_FIRST_HEADER not in sent
    assert _PROMPT in sent


@pytest.mark.unit
def test_bus_intent_states_the_text_marker_before_the_facts() -> None:
    """The marker leads the user turn (OMN-18349); the facts follow it."""
    sent = _intent_prompt("document", _PROMPT).removeprefix("/no_think\n")

    assert sent.index("### ANSWER") < sent.index(FACTS_FIRST_HEADER)


# ---------------------------------------------------------------------------
# In-process port: the request `onex delegate` makes
# ---------------------------------------------------------------------------


class _RecordingEffect:
    def __init__(self) -> None:
        self.calls: list[ModelLlmDelegationCallRequest] = []

    def __call__(
        self, request: ModelLlmDelegationCallRequest
    ) -> ModelLlmDelegationCallResult:
        self.calls.append(request)
        return ModelLlmDelegationCallResult(
            request_id=request.request_id,
            success=True,
            content=_ANSWER,
            tokens_in=11,
            tokens_out=22,
            latency_ms=5,
            actual_cost_usd=Decimal("0"),
            savings_usd=Decimal("0"),
        )


def _make_handler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[HandlerDelegateSkill, _RecordingEffect]:
    backends: list[dict[str, Any]] = [
        {
            "backend_id": "local-coder-mlx",
            "endpoint_url": "http://stickybeatz-studio:8401/v1/chat/completions",
            "model_name": "mlx-community/Qwen3.6-35B-A3B-8bit",
            "tier": "local",
            "max_tokens": 65536,
            "timeout_ms": 300000,
            "capabilities": ["review", "document", "planning", "test"],
        }
    ]
    monkeypatch.setattr(
        delegation_backend_resolution, "load_bifrost_backends", lambda **_: backends
    )
    effect = _RecordingEffect()
    port = LocalDelegationDispatchPort(
        effect_handler=effect,
        evidence_db_path=tmp_path / "d.sqlite",
        effect_process_boundary=False,
    )
    return HandlerDelegateSkill(dispatch_port=port), effect


@pytest.mark.unit
async def test_port_for_a_declaring_class_sends_the_facts_then_the_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handler, effect = _make_handler(tmp_path, monkeypatch)

    await handler.handle(
        ModelDelegateSkillRequest(
            prompt=_PROMPT,
            task_type="review",
            source="external-client",
            backend_id="local-coder-mlx",
        )
    )

    assert len(effect.calls) >= 1
    sent = effect.calls[0].prompt
    assert FACTS_FIRST_HEADER in sent
    assert f"{TASK_HEADER}\n{_PROMPT}" in sent
    assert "new 11: +    cap = 30" in sent


@pytest.mark.unit
@pytest.mark.parametrize("task_type", ["planning", "test"])
async def test_port_for_a_class_that_declares_no_shape_sends_the_prompt_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, task_type: str
) -> None:
    handler, effect = _make_handler(tmp_path, monkeypatch)

    await handler.handle(
        ModelDelegateSkillRequest(
            prompt=_PROMPT,
            task_type=task_type,
            source="external-client",
            backend_id="local-coder-mlx",
        )
    )

    assert len(effect.calls) >= 1
    sent = effect.calls[0].prompt
    assert FACTS_FIRST_HEADER not in sent
    assert _PROMPT in sent


@pytest.mark.unit
async def test_the_gate_grounds_on_what_the_model_was_told(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A line number the facts stated is grounded; the DoD still reads the caller's words."""
    handler, _ = _make_handler(tmp_path, monkeypatch)
    seen: dict[str, Any] = {}
    real = local_port_module.evaluate_quality_gate

    def spy(*args: Any, **kwargs: Any) -> Any:
        seen["grounding_source"] = kwargs.get("grounding_source")
        return real(*args, **kwargs)

    monkeypatch.setattr(local_port_module, "evaluate_quality_gate", spy)

    await handler.handle(
        ModelDelegateSkillRequest(
            prompt=_PROMPT,
            task_type="review",
            source="external-client",
            backend_id="local-coder-mlx",
        )
    )

    assert seen["grounding_source"] is not None
    assert seen["grounding_source"].startswith(FACTS_FIRST_HEADER)
    assert seen["grounding_source"].endswith(_PROMPT)
