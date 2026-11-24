# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-26365: ``reasoning_effort`` is a field of each backend's own row.

The value comes from the row (committed contract or overlay), is carried onto the
resolved backend and reaches the request that backend receives. A backend whose row
declares none sends none; a backend that declares one gets its own value, never a
neighbour's and never a literal written in code.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from omnimarket.enums.enum_provider_finish_reason import EnumProviderFinishReason
from omnimarket.handlers.routing.delegation_backend_resolution import (
    ModelResolvedDelegationBackend,
    resolve_delegation_backend,
)
from omnimarket.inference.protocol_config import with_backend_reasoning_effort
from omnimarket.models.delegation.wire.model_bifrost_delegation_config import (
    ModelDelegationBackendConfig,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_local_delegation_dispatch import (
    LocalDelegationDispatchPort,
)
from omnimarket.nodes.node_llm_delegation_call_effect import (
    ModelLlmDelegationCallRequest,
    ModelLlmDelegationCallResult,
)


def _row(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "backend_id": "local-inkling",
        "provider": "lab",
        "endpoint_url": "http://inference.example:8000/v1/chat/completions",
        "model_name": "Inkling-2-Mini",
        "tier": "local",
        "max_tokens": 8192,
        "timeout_ms": 60000,
        "capabilities": ["code_generation"],
    }
    base.update(overrides)
    return base


@pytest.mark.unit
def test_the_wire_row_declares_reasoning_effort_and_defaults_to_none() -> None:
    assert ModelDelegationBackendConfig.model_validate(_row()).reasoning_effort is None
    declared = ModelDelegationBackendConfig.model_validate(
        _row(reasoning_effort="medium")
    )
    assert declared.reasoning_effort == "medium"


@pytest.mark.unit
def test_a_blank_reasoning_effort_is_refused() -> None:
    with pytest.raises(ValidationError):
        ModelDelegationBackendConfig.model_validate(_row(reasoning_effort=""))


@pytest.mark.unit
def test_the_resolved_backend_carries_its_own_rows_value() -> None:
    row = _row(reasoning_effort="high")
    resolved = resolve_delegation_backend("code_generation", backends=[row])
    assert resolved.reasoning_effort == "high"
    plain = resolve_delegation_backend("code_generation", backends=[_row()])
    assert plain.reasoning_effort is None


@pytest.mark.unit
def test_the_helper_sets_the_rows_value_and_leaves_a_row_without_one_alone() -> None:
    profile_options = {"chat_template_kwargs": {"enable_thinking": False}}
    assert with_backend_reasoning_effort(profile_options, "low") == {
        "chat_template_kwargs": {"enable_thinking": False},
        "reasoning_effort": "low",
    }
    assert with_backend_reasoning_effort(profile_options, None) == profile_options
    assert "reasoning_effort" not in profile_options


@pytest.mark.unit
def test_the_row_value_wins_over_a_profile_that_matched_the_model_name() -> None:
    assert with_backend_reasoning_effort({"reasoning_effort": "none"}, "high") == {
        "reasoning_effort": "high"
    }


def _send(tmp_path: Path, backend: ModelResolvedDelegationBackend) -> dict[str, Any]:
    requests: list[ModelLlmDelegationCallRequest] = []

    def server(request: ModelLlmDelegationCallRequest) -> ModelLlmDelegationCallResult:
        requests.append(request)
        return ModelLlmDelegationCallResult(
            request_id=request.request_id,
            success=True,
            content="ok",
            finish_reason=EnumProviderFinishReason.STOP,
            tokens_in=10,
            tokens_out=2,
            latency_ms=5,
            actual_cost_usd=Decimal("0"),
            savings_usd=Decimal("0"),
        )

    port = LocalDelegationDispatchPort(
        effect_handler=server,
        evidence_db_path=tmp_path / f"{backend.backend_id}.sqlite",
        effect_process_boundary=False,
    )
    asyncio.run(
        port._run_single_attempt(
            backend=backend,
            prompt="Say ok.",
            task_type="document",
            correlation_id=uuid4(),
            max_tokens=2048,
            quality_contract_mode="extend_task_class",
            acceptance_criteria=(),
        )
    )
    assert len(requests) == 1
    return dict(requests[0].provider_request_options or {})


def _resolved(backend_id: str, effort: str | None) -> ModelResolvedDelegationBackend:
    return resolve_delegation_backend(
        "code_generation",
        backends=[_row(backend_id=backend_id, reasoning_effort=effort)]
        if effort is not None
        else [_row(backend_id=backend_id)],
    )


@pytest.mark.unit
def test_each_backend_gets_its_own_value_and_a_backend_without_one_gets_none(
    tmp_path: Path,
) -> None:
    high = _send(tmp_path, _resolved("local-a", "high"))
    low = _send(tmp_path, _resolved("local-b", "low"))
    none = _send(tmp_path, _resolved("local-c", None))
    assert high["reasoning_effort"] == "high"
    assert low["reasoning_effort"] == "low"
    assert "reasoning_effort" not in none
