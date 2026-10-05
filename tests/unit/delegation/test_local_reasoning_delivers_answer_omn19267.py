# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19267 AC1: a local reasoning delegation returns a non-empty deliverable.

The defect, measured 2026-09-23 on the lab model server (Qwen3.8-27B on vLLM,
no reasoning parser, runs 621c6a72 and 253031bb): with thinking on, the model
wrote its trace inline in ``message.content`` with no tags and no
``### ANSWER`` marker line, the deliverable extractor refused the response as
``ambiguous_unmarked_deliverable``, and the port blanked a correct answer on
every local attempt. With thinking off the model wrote its step-by-step
reasoning inside a marked deliverable, where the caller receives it.

These tests drive the real local dispatch port for the ``reasoning`` and
``complex_reasoning`` classes: the port's own system prompt, inference-protocol
shaping, deliverable extraction and refusal handling. Only the model server is
replaced, by a stand-in that answers the way the measured server did: the
untagged trace when the outbound request leaves thinking on, a marked answer
when the request carries ``chat_template_kwargs.enable_thinking: false``. The
class therefore reaches the caller non-empty only if the port sends it with
thinking off, which is the OMN-19267 profile change in
``inference_protocols.v1.yaml`` (omnimarket#2809). With that change reverted the
request leaves thinking on, the stand-in returns the measured trace, and the
extractor refuses it, so these tests fail.

The live-endpoint half of AC1 (that the served model itself answers with a
marker once thinking is off) cannot run in hosted CI; it is the lab probe
recorded on the OCC receipt for the same criterion.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from pathlib import Path
from typing import Any, Final
from uuid import uuid4

import pytest

from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_local_delegation_dispatch import (
    LocalDelegationDispatchPort,
)
from omnimarket.nodes.node_llm_delegation_call_effect import (
    ModelLlmDelegationCallRequest,
    ModelLlmDelegationCallResult,
)
from omnimarket.routing.delegation_backend_resolution import (
    ModelResolvedDelegationBackend,
)

# The backend and model the defect was measured on. The thinking-suppressing
# profile matches on the model name, so the model id is load-bearing.
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

# Thinking on: the trace arrives inline and untagged, and the model answers
# after it without the marker line the class contract asks for. This is the
# shape of the bytes measured on 2026-09-23 (raw content opening
# "We need to respond to user:", message.reasoning null).
_THINKING_ON_CONTENT: Final[str] = (
    "We need to respond to user: they ask whether every bloop is a lazzie. "
    "Every bloop is a razzie, every razzie is a lazzie, so by transitivity "
    "yes. Need to state the conclusion.\n\n"
    "Yes. Every bloop is a lazzie."
)

# Thinking off: the reasoning is written inside the marked deliverable.
_THINKING_OFF_CONTENT: Final[str] = (
    "### ANSWER\n"
    "1. Every bloop is a razzie.\n"
    "2. Every razzie is a lazzie.\n"
    "3. Set inclusion is transitive, so every bloop is a lazzie.\n\n"
    "Conclusion: yes, every bloop is a lazzie."
)


def _thinking_suppressed(request: ModelLlmDelegationCallRequest) -> bool:
    options: dict[str, Any] = dict(request.provider_request_options or {})
    kwargs = options.get("chat_template_kwargs")
    return isinstance(kwargs, dict) and kwargs.get("enable_thinking") is False


class _MeasuredModelServer:
    """Answers as the measured lab server did, keyed on the request's thinking state."""

    def __init__(self) -> None:
        self.requests: list[ModelLlmDelegationCallRequest] = []

    def __call__(
        self, request: ModelLlmDelegationCallRequest
    ) -> ModelLlmDelegationCallResult:
        self.requests.append(request)
        content = (
            _THINKING_OFF_CONTENT
            if _thinking_suppressed(request)
            else _THINKING_ON_CONTENT
        )
        return ModelLlmDelegationCallResult(
            request_id=request.request_id,
            success=True,
            content=content,
            tokens_in=117,
            tokens_out=64,
            latency_ms=5,
            actual_cost_usd=Decimal("0"),
            savings_usd=Decimal("0"),
        )


def _run_reasoning_attempt(tmp_path: Path, task_type: str) -> tuple[Any, Any]:
    server = _MeasuredModelServer()
    port = LocalDelegationDispatchPort(
        effect_handler=server,
        evidence_db_path=tmp_path / "evidence.sqlite",
        effect_process_boundary=False,
    )
    outcome = asyncio.run(
        port._run_single_attempt(
            backend=_LOCAL_REASONING_BACKEND,
            prompt=_PROMPT,
            task_type=task_type,
            correlation_id=uuid4(),
            max_tokens=1024,
            quality_contract_mode="extend_task_class",
            acceptance_criteria=(),
        )
    )
    assert len(server.requests) == 1, server.requests
    return outcome, server.requests[0]


@pytest.mark.unit
@pytest.mark.parametrize("task_type", ["reasoning", "complex_reasoning"])
def test_local_reasoning_delegation_returns_a_non_empty_deliverable(
    tmp_path: Path, task_type: str
) -> None:
    """AC1: non-empty deliverable, no ambiguous_unmarked_deliverable refusal."""

    outcome, request = _run_reasoning_attempt(tmp_path, task_type)

    assert _thinking_suppressed(request), (
        f"the {task_type!r} class reached the local model with thinking on "
        "(provider_request_options="
        f"{request.provider_request_options!r}); on the measured server that "
        "returns an untagged trace with no marker, which is blanked"
    )
    assert outcome.output_refusal is None, (
        f"the {task_type!r} delegation was refused: {outcome.output_refusal!r}"
    )
    assert outcome.result is not None
    deliverable = outcome.result.content or ""
    assert deliverable.strip(), (
        f"the {task_type!r} delegation returned an empty deliverable"
    )
    assert "every bloop is a lazzie" in deliverable
    assert "We need to respond to user" not in deliverable


@pytest.mark.unit
def test_the_measured_thinking_on_response_is_the_refused_shape(
    tmp_path: Path,
) -> None:
    """Positive control: the stand-in's thinking-on bytes are what the port blanks.

    Without this, a change that made the extractor accept the trace would let
    the test above pass for the wrong reason, and a stand-in whose thinking-on
    reply the port accepted would prove nothing.
    """

    port = LocalDelegationDispatchPort(
        effect_handler=lambda request: ModelLlmDelegationCallResult(
            request_id=request.request_id,
            success=True,
            content=_THINKING_ON_CONTENT,
            tokens_in=117,
            tokens_out=64,
            latency_ms=5,
            actual_cost_usd=Decimal("0"),
            savings_usd=Decimal("0"),
        ),
        evidence_db_path=tmp_path / "evidence.sqlite",
        effect_process_boundary=False,
    )
    outcome = asyncio.run(
        port._run_single_attempt(
            backend=_LOCAL_REASONING_BACKEND,
            prompt=_PROMPT,
            task_type="reasoning",
            correlation_id=uuid4(),
            max_tokens=1024,
            quality_contract_mode="extend_task_class",
            acceptance_criteria=(),
        )
    )
    assert outcome.output_refusal is not None
    assert outcome.output_refusal.reason.value == "ambiguous_unmarked_deliverable"
    assert outcome.result is not None
    assert (outcome.result.content or "") == ""
