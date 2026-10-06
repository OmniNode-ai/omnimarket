# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The local dispatch carries the contract and never consumes a second tag."""

import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from omnimarket.delegation.reasoning_preamble import (
    LEADING_REASONING_TRACE_CHECK_NAME,
    RESIDUAL_REASONING_TAG_CHECK_NAME,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_local_delegation_dispatch import (
    LocalDelegationDispatchPort,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers import transport
from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_llm_delegation_call import (
    HandlerLlmDelegationCall,
)
from omnimarket.routing.delegation_backend_resolution import resolve_delegation_backend

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("terminator", "answer", "passed"),
    [
        ("</think>", "The answer.", False),
        (None, "The answer.", False),
        ("</think>", "Part one.</think> more reasoning. Final.", False),
        ("</think>", "Final answer with <think> inside", False),
        ("</think>", "Part one. <think>more reasoning</think> Final.", False),
    ],
)
async def test_adapter_receipt_prevents_second_segmentation_in_local_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    terminator: str | None,
    answer: str,
    passed: bool,
) -> None:
    raw = "We need answer user...</think>\n\n" + json.dumps({"answer": answer})
    monkeypatch.setattr(
        transport, "probe_served_models", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        "omnimarket.nodes.node_llm_delegation_call_effect.handlers."
        "handler_llm_delegation_call._is_endpoint_healthy",
        lambda *_args, **_kwargs: True,
    )

    def fake_post(**_kwargs: Any) -> transport.ModelTransportResponse:
        return transport.ModelTransportResponse(
            status_code=200,
            json_body={
                "choices": [{"message": {"content": raw}, "finish_reason": "stop"}]
            },
            latency_ms=7,
        )

    monkeypatch.setattr(transport, "post_chat_completion", fake_post)
    backend = resolve_delegation_backend(
        "document",
        backends=[
            {
                "backend_id": "local-coder",
                "model_name": "test-qwen",
                "endpoint_url": "https://inference.example/v1/chat/completions",
                "tier": "local",
                "capabilities": ["document"],
                "max_tokens": 4096,
                "timeout_ms": 60000,
                "inline_reasoning_terminator": terminator,
            }
        ],
    )
    port = LocalDelegationDispatchPort(
        effect_handler=HandlerLlmDelegationCall(),
        effect_process_boundary=False,
        evidence_db_path=tmp_path / "evidence.sqlite",
    )
    outcome = await port._run_single_attempt(
        backend=backend,
        prompt="Return an object with an answer field.",
        task_type="document",
        correlation_id=uuid4(),
        max_tokens=None,
        quality_contract_mode="extend_task_class",
        acceptance_criteria=(),
        response_contract={
            "type": "object",
            "properties": {"answer": {"type": "string"}},
            "required": ["answer"],
        },
    )
    assert outcome.result is not None
    assert outcome.result.success
    assert (outcome.result.reasoning_stripped_chars > 0) == (terminator is not None)
    assert outcome.result.content is not None
    assert json.loads(outcome.result.content) == {"answer": answer}
    assert outcome.gate_result is not None
    assert outcome.gate_result.passed == passed
    if not passed:
        assert outcome.gate_result.rule_evaluations[0].rule == (
            LEADING_REASONING_TRACE_CHECK_NAME
            if answer == "The answer."
            else RESIDUAL_REASONING_TAG_CHECK_NAME
        )
