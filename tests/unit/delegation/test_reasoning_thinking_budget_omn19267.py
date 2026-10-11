# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19267: bound reasoning traces while requiring a non-empty answer.

The 2026-10-10 GPU rung measurements fit the effect's per-call ceiling with a
2048-token thinking budget. Both server spellings must travel with the request,
and a response that spent the whole budget on thinking must fail the class gate.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from pathlib import Path
from typing import Final
from uuid import uuid4

import pytest

from omnimarket.enums.enum_provider_finish_reason import EnumProviderFinishReason
from omnimarket.inference.protocol_config import (
    apply_inference_protocol,
    load_inference_protocol_config,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_local_delegation_dispatch import (
    LocalDelegationDispatchPort,
)
from omnimarket.nodes.node_llm_delegation_call_effect import (
    ModelLlmDelegationCallRequest,
    ModelLlmDelegationCallResult,
)
from omnimarket.nodes.node_llm_delegation_call_effect.models.model_inference_call_budget import (
    load_inference_call_budget,
)
from omnimarket.routing.delegation_backend_resolution import (
    ModelResolvedDelegationBackend,
)

# The slowest capped call measured 2026-10-10 on a GPU rung under load:
# 4250 output tokens in 123.1 s on the vLLM rung (budget 4096).
_MEASURED_GPU_RUNG_FLOOR_TOKENS_PER_SECOND: Final[int] = 34

# The largest answer after the thinking cap in the same measurement was
# 389 tokens (4485 output tokens at budget 4096).
_ANSWER_ALLOWANCE_TOKENS: Final[int] = 512

# Same backend fixture as test_local_reasoning_delivers_answer_omn19267.py,
# including its 4096-token maximum and 30000 ms timeout. The model id matches
# the bounded reasoning profile.
_LOCAL_REASONING_BACKEND: Final = ModelResolvedDelegationBackend(
    backend_id="local-heavy-reasoning",
    model_id="Qwen3.8-27B",
    endpoint_ref="https://local.example/v1/chat/completions",
    tier="local",
    max_tokens=4096,
    timeout_ms=30000,
)

_PROMPT: Final[str] = (
    "Reason step by step: if every bloop is a razzie and every razzie is a "
    "lazzie, is every bloop a lazzie? State the conclusion."
)


@pytest.mark.unit
def test_reasoning_profile_bounds_thinking_under_both_server_spellings() -> None:
    """Thinking stays on with equal positive budgets for both servers."""
    _, _, request_options = apply_inference_protocol(
        system_prompt="You are a helpful assistant.",
        prompt="Reason about it.",
        model="Qwen3.8-27B",
        task_type="reasoning",
        backend_id="local-heavy-reasoning",
        config=load_inference_protocol_config(),
    )

    assert request_options["chat_template_kwargs"]["enable_thinking"] is True, (
        f"reasoning thinking was disabled: {request_options!r}"
    )
    budget = request_options.get("thinking_token_budget")
    assert isinstance(budget, int), (
        f"vLLM thinking_token_budget is not an integer: {budget!r}"
    )
    assert budget > 0, f"vLLM thinking_token_budget is not positive: {budget!r}"
    assert request_options.get("thinking_budget_tokens") == budget, (
        "llama.cpp thinking_budget_tokens differs from vLLM thinking_token_budget: "
        f"{request_options!r}"
    )


@pytest.mark.unit
def test_reasoning_thinking_budget_fits_the_per_call_ceiling() -> None:
    """At 4096 this fails (4608 > 4080), matching the measured 123.1 s call."""
    _, _, request_options = apply_inference_protocol(
        system_prompt="You are a helpful assistant.",
        prompt="Reason about it.",
        model="Qwen3.8-27B",
        task_type="reasoning",
        backend_id="local-heavy-reasoning",
        config=load_inference_protocol_config(),
    )
    budget = request_options.get("thinking_token_budget")
    assert isinstance(budget, int), (
        f"thinking_token_budget is not an integer: {budget!r}"
    )
    ceiling = load_inference_call_budget().ceiling_for("Qwen3.8-27B")

    assert (
        budget + _ANSWER_ALLOWANCE_TOKENS
        <= ceiling * _MEASURED_GPU_RUNG_FLOOR_TOKENS_PER_SECOND
    ), (
        f"thinking budget {budget} plus answer allowance {_ANSWER_ALLOWANCE_TOKENS} "
        f"exceeds the {ceiling} s per-call ceiling at "
        f"{_MEASURED_GPU_RUNG_FLOOR_TOKENS_PER_SECOND} tokens/s"
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "finish_reason", [EnumProviderFinishReason.LENGTH, EnumProviderFinishReason.STOP]
)
def test_a_truncated_or_empty_thinking_response_is_never_accepted(
    tmp_path: Path, finish_reason: EnumProviderFinishReason
) -> None:
    """Guard: the port already refuses both shapes.

    Pin that a capped trace with no answer is a failure with a reason, never an
    empty success.
    """
    requests: list[ModelLlmDelegationCallRequest] = []

    def server(request: ModelLlmDelegationCallRequest) -> ModelLlmDelegationCallResult:
        requests.append(request)
        # The whole configured 2048-token budget went to the trace. Input tokens
        # (117), latency (5 ms), and zero costs follow the reference stand-in.
        return ModelLlmDelegationCallResult(
            request_id=request.request_id,
            success=True,
            content="",
            finish_reason=finish_reason,
            tokens_in=117,
            tokens_out=2048,
            latency_ms=5,
            actual_cost_usd=Decimal("0"),
            savings_usd=Decimal("0"),
        )

    port = LocalDelegationDispatchPort(
        effect_handler=server,
        evidence_db_path=tmp_path / "evidence.sqlite",
        effect_process_boundary=False,
    )
    outcome = asyncio.run(
        port._run_single_attempt(
            backend=_LOCAL_REASONING_BACKEND,
            prompt=_PROMPT,
            task_type="reasoning",
            correlation_id=uuid4(),
            # Same requested output maximum as the reference reasoning attempt.
            max_tokens=1024,
            quality_contract_mode="extend_task_class",
            acceptance_criteria=(),
        )
    )

    assert len(requests) == 1, f"expected one effect request, got {requests!r}"
    options = dict(requests[0].provider_request_options or {})
    assert "thinking_token_budget" in options, (
        f"the effect request did not carry the bounded thinking profile: {options!r}"
    )
    assert outcome.gate_result is not None, (
        f"empty {finish_reason.value} response had no gate result"
    )
    assert outcome.gate_result.passed is False, (
        f"empty {finish_reason.value} response was accepted: {outcome.gate_result!r}"
    )
    assert outcome.gate_result.failure_reasons, (
        f"empty {finish_reason.value} response failed without a reason: "
        f"{outcome.gate_result!r}"
    )
