# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``no_escalation`` is a declared delegate-skill request field (OMN-18931, step 2).

omnibase_infra's dogfood fault-route port publishes a pinned request with
``backend_id``, ``requested_timeout_seconds`` and ``no_escalation: true``. Step 1
released a consumer that decodes the key without declaring it. This step
declares it, and pins the rollout properties that keep it safe:

* the key reaches the wire only when true, so an ordinary request is unchanged;
* the handler passes it to the dispatch port only when true, because the
  runtime wiring injects omnibase_infra's own port, and a released port that
  predates the keyword must keep serving every ordinary request;
* a port that cannot honour the policy refuses it instead of dropping it.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from omnimarket.models.delegation.wire.model_delegate_skill_request import (
    ModelDelegateSkillRequest,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.handler_delegate_skill import (
    HandlerDelegateSkill,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_local_delegation_dispatch import (
    LocalDelegationDispatchPort,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_runtime_delegation_dispatch import (
    RuntimeDelegationDispatchPort,
)

pytestmark = pytest.mark.unit

_FAULT_BACKEND = "dogfood-fault-429"


def _request(**overrides: object) -> ModelDelegateSkillRequest:
    payload: dict[str, object] = {
        "prompt": "exercise the declared dogfood fault route",
        "task_type": "document",
        "source": "codex",
    }
    payload.update(overrides)
    return ModelDelegateSkillRequest.model_validate(payload)


def _fault_request() -> ModelDelegateSkillRequest:
    return _request(
        backend_id=_FAULT_BACKEND, requested_timeout_seconds=240, no_escalation=True
    )


_TERMINAL: dict[str, object] = {
    "status": "completed",
    "content": "accepted",
    "delegated_to": "test",
    "model_name": "test-model",
    "quality_gate_passed": True,
    "quality_score": 1.0,
}


class _CapturingPort:
    """A port that accepts any keyword, so it records what the handler sent."""

    def __init__(self) -> None:
        self.kwargs: dict[str, object] = {}

    async def dispatch(self, **kwargs: Any) -> dict[str, object]:
        self.kwargs = kwargs
        return dict(_TERMINAL)


class _PortPredatingTheKeyword:
    """The released omnibase_infra port shape: no ``no_escalation`` keyword."""

    def __init__(self) -> None:
        self.calls = 0

    async def dispatch(
        self,
        *,
        prompt: str,
        task_type: str,
        correlation_id: object,
        max_tokens: int | None,
        source_file_path: str | None,
        source_session_id: str | None,
        wait: bool,
        execution_timeout_seconds: int,
        terminal_delivery_margin_seconds: int,
        quality_contract_mode: str,
        acceptance_criteria: tuple[str, ...],
        tenant_id: str | None,
        provenance: object = None,
        backend_id: str | None = None,
        response_contract: dict[str, object] | None = None,
        system_prompt: str | None = None,
        temperature: float | None = None,
        response_format: dict[str, object] | None = None,
    ) -> dict[str, object]:
        self.calls += 1
        return dict(_TERMINAL)


def test_the_fault_request_round_trips_all_three_fields() -> None:
    consumed = ModelDelegateSkillRequest.model_validate_json(
        _fault_request().model_dump_json()
    )
    assert consumed.backend_id == _FAULT_BACKEND
    assert consumed.requested_timeout_seconds == 240
    assert consumed.no_escalation is True


def test_an_ordinary_request_emits_no_new_key() -> None:
    wire = json.loads(_request().model_dump_json())
    assert "no_escalation" not in wire
    assert "requested_timeout_seconds" not in wire


def test_an_explicit_false_is_omitted_too() -> None:
    wire = json.loads(_request(no_escalation=False).model_dump_json())
    assert "no_escalation" not in wire


def test_no_escalation_requires_a_backend_pin() -> None:
    with pytest.raises(ValidationError, match="requires backend_id"):
        _request(no_escalation=True)


@pytest.mark.asyncio
async def test_the_handler_threads_true_to_the_dispatch_port() -> None:
    port = _CapturingPort()
    terminal = await HandlerDelegateSkill(dispatch_port=port).handle(  # type: ignore[arg-type]
        _fault_request()
    )
    assert terminal.status == "completed"
    assert port.kwargs["no_escalation"] is True
    assert port.kwargs["backend_id"] == _FAULT_BACKEND
    assert port.kwargs["execution_timeout_seconds"] == 240


@pytest.mark.asyncio
async def test_the_handler_sends_no_keyword_for_an_ordinary_request() -> None:
    port = _CapturingPort()
    await HandlerDelegateSkill(dispatch_port=port).handle(_request())  # type: ignore[arg-type]
    assert "no_escalation" not in port.kwargs


@pytest.mark.asyncio
async def test_a_port_predating_the_keyword_still_serves_an_ordinary_request() -> None:
    port = _PortPredatingTheKeyword()
    terminal = await HandlerDelegateSkill(dispatch_port=port).handle(  # type: ignore[arg-type]
        _request(backend_id=_FAULT_BACKEND, requested_timeout_seconds=240)
    )
    assert port.calls == 1
    assert terminal.status == "completed"


@pytest.mark.asyncio
async def test_a_port_predating_the_keyword_does_not_run_a_fault_request() -> None:
    port = _PortPredatingTheKeyword()
    terminal = await HandlerDelegateSkill(dispatch_port=port).handle(  # type: ignore[arg-type]
        _fault_request()
    )
    assert port.calls == 0
    assert terminal.status != "completed"


def _port_kwargs() -> dict[str, Any]:
    return {
        "prompt": "exercise the declared dogfood fault route",
        "task_type": "document",
        "correlation_id": uuid4(),
        "max_tokens": 64,
        "source_file_path": None,
        "source_session_id": None,
        "wait": False,
        "execution_timeout_seconds": 240,
        "terminal_delivery_margin_seconds": 60,
        "quality_contract_mode": "extend_task_class",
        "acceptance_criteria": (),
        "tenant_id": None,
        "backend_id": _FAULT_BACKEND,
        "no_escalation": True,
    }


@pytest.mark.asyncio
async def test_the_in_process_port_refuses_the_policy_by_name() -> None:
    port = LocalDelegationDispatchPort(effect_handler=object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="no_escalation"):
        await port.dispatch(**_port_kwargs())


@pytest.mark.asyncio
async def test_the_market_runtime_port_refuses_the_policy_by_name() -> None:
    class _Bus:
        published: list[bytes] = []

        async def publish(
            self, _topic: str, _key: bytes | None, value: bytes, _headers: object
        ) -> None:
            self.published.append(value)

    bus = _Bus()
    port = RuntimeDelegationDispatchPort(event_bus=bus)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="no_escalation"):
        await port.dispatch(**_port_kwargs())
    assert bus.published == []
