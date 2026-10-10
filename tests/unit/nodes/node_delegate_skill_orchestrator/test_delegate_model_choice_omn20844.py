# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20844: ``onex delegate --model`` overrides the customer route's model per call.

The request field is declared consumer-first and omitted from serialisation
when unset, so a request without it is byte-identical to today's. The handler
passes it to the dispatch port only when set (a port that predates the keyword
keeps serving every other request). The in-process port runs the customer's own
BYOK route on the named model, so the result and the receipt name it; a model
named for a route that is not the customer's own key is refused, never applied
to a house or local rung. The runtime port's canonical request cannot carry it,
so it refuses rather than dropping it.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from pydantic import ValidationError

from omnimarket.models.delegation.wire.model_delegate_skill_request import (
    ModelDelegateSkillRequest,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.handler_delegate_skill import (
    HandlerDelegateSkill,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports import (
    port_local_delegation_dispatch as port_mod,
)
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

pytestmark = pytest.mark.unit

PAID = "openai/gpt-5-nano"
STORED = "google/gemma-4-31b-it:free"
REF = f"cred_acme_openrouter_{'a' * 32}"
_ANSWER = (
    "### ANSWER\n"
    "According to Smith (2020) and the theorem in section 3, the tradeoff is "
    "significant because the evidence shows X; therefore we conclude Y. See "
    "references [12] for the methodical analysis and the risk profile."
)


class TestRequestField:
    def test_the_model_field_is_accepted(self) -> None:
        request = ModelDelegateSkillRequest(
            prompt="p", task_type="research", source="claude-code", model=PAID
        )
        assert request.model == PAID
        assert request.model_dump()["model"] == PAID

    def test_an_unset_model_is_omitted_from_the_wire(self) -> None:
        request = ModelDelegateSkillRequest(
            prompt="p", task_type="research", source="claude-code"
        )
        assert "model" not in request.model_dump()
        assert '"model"' not in request.model_dump_json()

    def test_an_empty_model_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            ModelDelegateSkillRequest(
                prompt="p", task_type="research", source="claude-code", model=""
            )


class TestHandlerSeam:
    async def test_the_handler_passes_the_model_to_the_dispatch_port(self) -> None:
        port = AsyncMock()
        port.dispatch.return_value = {"status": "completed", "content": "ok"}
        handler = HandlerDelegateSkill(object(), dispatch_port=port)
        await handler.handle(
            ModelDelegateSkillRequest(
                prompt="p",
                task_type="research",
                source="claude-code",
                backend_id="byok-openrouter",
                model=PAID,
            )
        )
        assert port.dispatch.await_args.kwargs["model"] == PAID

    async def test_no_model_keyword_reaches_a_port_when_unset(self) -> None:
        port = AsyncMock()
        port.dispatch.return_value = {"status": "completed", "content": "ok"}
        handler = HandlerDelegateSkill(object(), dispatch_port=port)
        await handler.handle(
            ModelDelegateSkillRequest(
                prompt="p", task_type="research", source="claude-code"
            )
        )
        assert "model" not in port.dispatch.await_args.kwargs


class _Effect:
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


def _byok_route() -> ModelResolvedDelegationBackend:
    return ModelResolvedDelegationBackend(
        backend_id="byok-openrouter",
        model_id=STORED,
        endpoint_ref="https://openrouter.ai/api/v1/chat/completions",
        tier="cheap_cloud",
        max_tokens=4096,
        timeout_ms=30000,
        secret_ref=REF,
    )


def _house_rung() -> ModelResolvedDelegationBackend:
    return ModelResolvedDelegationBackend(
        backend_id="local-coder",
        model_id="Qwen3.8-27B",
        endpoint_ref="https://local.example/v1/chat/completions",
        tier="local",
        max_tokens=4096,
        timeout_ms=30000,
    )


def _dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, route: Any, **kwargs: Any
) -> tuple[dict[str, object], _Effect]:
    monkeypatch.setattr(port_mod, "resolve_delegation_backend", lambda *_a, **_k: route)
    effect = _Effect()
    port = LocalDelegationDispatchPort(
        effect_handler=effect,
        evidence_db_path=tmp_path / "d.sqlite",
        effect_process_boundary=False,
    )
    result = asyncio.run(
        port.dispatch(
            prompt="explain the tradeoff",
            task_type="research",
            correlation_id=uuid4(),
            max_tokens=256,
            source_file_path=None,
            source_session_id=None,
            wait=True,
            execution_timeout_seconds=240,
            terminal_delivery_margin_seconds=60,
            quality_contract_mode="extend_task_class",
            acceptance_criteria=(),
            tenant_id="acme",
            backend_id="byok-openrouter",
            **kwargs,
        )
    )
    return result, effect


class TestLocalPort:
    def test_the_customer_route_runs_the_named_model_and_the_result_names_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result, effect = _dispatch(tmp_path, monkeypatch, _byok_route(), model=PAID)

        assert [call.model_id for call in effect.calls] == [PAID]
        assert result["model_name"] == PAID

    def test_without_a_model_the_stored_model_runs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result, effect = _dispatch(tmp_path, monkeypatch, _byok_route())

        assert [call.model_id for call in effect.calls] == [STORED]
        assert result["model_name"] == STORED

    def test_a_model_for_a_route_that_is_not_the_customers_key_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with pytest.raises(ValueError, match="--model"):
            _dispatch(tmp_path, monkeypatch, _house_rung(), model=PAID)


class TestRuntimePort:
    async def test_the_runtime_port_refuses_a_model_rather_than_dropping_it(
        self,
    ) -> None:
        from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_runtime_delegation_dispatch import (
            RuntimeDelegationDispatchPort,
        )

        port = RuntimeDelegationDispatchPort.__new__(RuntimeDelegationDispatchPort)
        with pytest.raises(ValueError, match="model"):
            await port.dispatch(
                prompt="p",
                task_type="research",
                correlation_id=uuid4(),
                max_tokens=None,
                source_file_path=None,
                source_session_id=None,
                wait=True,
                execution_timeout_seconds=60,
                terminal_delivery_margin_seconds=10,
                quality_contract_mode="extend_task_class",
                acceptance_criteria=(),
                tenant_id=None,
                model=PAID,
            )
