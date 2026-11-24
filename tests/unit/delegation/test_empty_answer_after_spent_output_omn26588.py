# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-26588: an empty answer after spent output tokens is named a spent output budget.

A local model that reasons by default can spend its whole completion budget on
thinking and return empty ``content`` with a provider stop reason other than
``length``. The gate read that as ``MALFORMED: empty response``, the verdict the
reducer deliberately does not escalate on. The tokens the provider billed say the
model did work and ran out of answer, which is the shape the next rung (its own
budget) recovers from, so it carries the ``WEAK_OUTPUT`` prefix. An empty answer
that billed no output tokens stays ``MALFORMED``.
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
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_local_delegation_dispatch import (
    LocalDelegationDispatchPort,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate import (
    delta as quality_gate_delta,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_quality_gate_input import (
    ModelQualityGateInput,
)
from omnimarket.nodes.node_llm_delegation_call_effect import (
    ModelLlmDelegationCallRequest,
    ModelLlmDelegationCallResult,
)

_CODE_CHECKS = ("output_parses", "compiles_without_errors", "code_artifact_present")


def _gate_input(
    content: str, checks: tuple[str, ...] = _CODE_CHECKS
) -> ModelQualityGateInput:
    return ModelQualityGateInput(
        correlation_id=uuid4(),
        task_type="code_generation",
        llm_response_content=content,
        dod_deterministic=checks,
    )


@pytest.mark.unit
def test_empty_answer_after_spent_output_tokens_is_a_climbable_verdict() -> None:
    result = quality_gate_delta(
        _gate_input(""),
        finish_reason=EnumProviderFinishReason.STOP,
        output_tokens_spent=8192,
    )
    assert result.passed is False
    assert result.fallback_recommended is True, result
    assert result.failure_reasons[0].startswith("WEAK_OUTPUT"), result.failure_reasons
    assert "8192" in result.failure_reasons[0], result.failure_reasons


@pytest.mark.unit
def test_empty_answer_with_no_output_tokens_stays_malformed() -> None:
    result = quality_gate_delta(
        _gate_input("", ("output_parses",)),
        finish_reason=EnumProviderFinishReason.STOP,
        output_tokens_spent=0,
    )
    assert result.passed is False
    assert any(r.startswith("MALFORMED") for r in result.failure_reasons), result
    assert not any(r.startswith("WEAK_OUTPUT") for r in result.failure_reasons), result


@pytest.mark.unit
def test_a_non_empty_answer_is_not_touched_by_the_spent_tokens() -> None:
    result = quality_gate_delta(
        _gate_input("```python\ndef f() -> int:\n    return 1\n```\n"),
        finish_reason=EnumProviderFinishReason.STOP,
        output_tokens_spent=8192,
    )
    assert not any(r.startswith("WEAK_OUTPUT") for r in result.failure_reasons), result


@pytest.mark.unit
def test_dispatch_port_threads_the_billed_output_tokens_into_the_gate(
    tmp_path: Path,
) -> None:
    def server(request: ModelLlmDelegationCallRequest) -> ModelLlmDelegationCallResult:
        return ModelLlmDelegationCallResult(
            request_id=request.request_id,
            success=True,
            content="",
            finish_reason=EnumProviderFinishReason.STOP,
            tokens_in=117,
            tokens_out=8192,
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
            backend=ModelResolvedDelegationBackend(
                backend_id="local-coder",
                model_id="Qwen3.8-27B",
                endpoint_ref="https://local.example/v1/chat/completions",
                tier="local",
                max_tokens=8192,
                timeout_ms=30000,
            ),
            prompt="Write a function that adds two integers.",
            task_type="code_generation",
            correlation_id=uuid4(),
            max_tokens=8192,
            quality_contract_mode="extend_task_class",
            acceptance_criteria=(),
        )
    )
    assert outcome.gate_result is not None
    assert outcome.gate_result.passed is False
    assert outcome.gate_result.fallback_recommended is True, outcome.gate_result
    assert any(
        r.startswith("WEAK_OUTPUT") for r in outcome.gate_result.failure_reasons
    ), outcome.gate_result.failure_reasons
