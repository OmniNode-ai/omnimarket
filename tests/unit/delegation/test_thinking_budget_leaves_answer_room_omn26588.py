# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-26588: a thinking budget never takes the whole output cap.

Thinking tokens come out of the same completion budget as the answer. A thinking
budget at or above the output cap leaves the answer no tokens: the model reasons
until ``length`` and returns empty content. Whatever sets the budget (a protocol
profile or the code-budget overlay's ``class_sampling``), the request that leaves
the port carries a thinking budget of at most half the output cap, so the answer
keeps at least as many tokens as the reasoning.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from omnimarket.enums.enum_provider_finish_reason import EnumProviderFinishReason
from omnimarket.handlers.routing.delegation_backend_resolution import (
    ModelResolvedDelegationBackend,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.handler_thinking_budget import (
    bound_thinking_budget_to_answer_room,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_local_delegation_dispatch import (
    LocalDelegationDispatchPort,
)
from omnimarket.nodes.node_llm_delegation_call_effect import (
    ModelLlmDelegationCallRequest,
    ModelLlmDelegationCallResult,
)


@pytest.mark.unit
def test_budget_at_the_output_cap_is_cut_to_half_of_it() -> None:
    bounded = bound_thinking_budget_to_answer_room(
        {"thinking_token_budget": 8192, "thinking_budget_tokens": 8192, "top_p": 0.9},
        max_tokens=8192,
    )
    assert bounded == {
        "thinking_token_budget": 4096,
        "thinking_budget_tokens": 4096,
        "top_p": 0.9,
    }


@pytest.mark.unit
def test_budget_within_half_the_cap_is_left_alone() -> None:
    options = {"thinking_token_budget": 2048, "thinking_budget_tokens": 2048}
    assert bound_thinking_budget_to_answer_room(options, max_tokens=4096) == options


@pytest.mark.unit
def test_options_without_a_budget_are_returned_unchanged() -> None:
    options = {"chat_template_kwargs": {"enable_thinking": False}}
    assert bound_thinking_budget_to_answer_room(options, max_tokens=8192) == options


@pytest.mark.unit
def test_the_input_options_are_never_changed() -> None:
    options = {"thinking_token_budget": 8192}
    bound_thinking_budget_to_answer_room(options, max_tokens=8192)
    assert options == {"thinking_token_budget": 8192}


@pytest.mark.unit
def test_a_cap_of_one_token_still_sends_a_positive_budget() -> None:
    bounded = bound_thinking_budget_to_answer_room(
        {"thinking_token_budget": 2048}, max_tokens=1
    )
    assert bounded == {"thinking_token_budget": 1}


@pytest.mark.unit
def test_the_request_the_port_sends_leaves_the_answer_room(tmp_path: Path) -> None:
    """The ``reasoning`` profile sets 2048; a 2048 output cap leaves 1024 for the answer."""
    requests: list[ModelLlmDelegationCallRequest] = []

    def server(request: ModelLlmDelegationCallRequest) -> ModelLlmDelegationCallResult:
        requests.append(request)
        return ModelLlmDelegationCallResult(
            request_id=request.request_id,
            success=True,
            content="Yes: every bloop is a lazzie.",
            finish_reason=EnumProviderFinishReason.STOP,
            tokens_in=117,
            tokens_out=40,
            latency_ms=5,
            actual_cost_usd=Decimal("0"),
            savings_usd=Decimal("0"),
        )

    port = LocalDelegationDispatchPort(
        effect_handler=server,
        evidence_db_path=tmp_path / "evidence.sqlite",
        effect_process_boundary=False,
    )
    asyncio.run(
        port._run_single_attempt(
            backend=ModelResolvedDelegationBackend(
                backend_id="local-heavy-reasoning",
                model_id="Qwen3.8-27B",
                endpoint_ref="https://local.example/v1/chat/completions",
                tier="local",
                max_tokens=2048,
                timeout_ms=30000,
            ),
            prompt="Reason step by step: is every bloop a lazzie?",
            task_type="reasoning",
            correlation_id=uuid4(),
            max_tokens=2048,
            quality_contract_mode="extend_task_class",
            acceptance_criteria=(),
        )
    )
    assert len(requests) == 1
    options = dict(requests[0].provider_request_options or {})
    assert requests[0].max_tokens is not None
    cap = requests[0].max_tokens
    assert options["thinking_token_budget"] <= cap // 2, (cap, options)
    assert options["thinking_budget_tokens"] == options["thinking_token_budget"]
