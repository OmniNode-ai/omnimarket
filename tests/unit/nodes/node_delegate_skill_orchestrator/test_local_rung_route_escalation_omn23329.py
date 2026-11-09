# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23329: escalation off the lead rung follows a measured quality miss.

The small local model is the first rung of the ``local`` tier for the classes it
serves. When its answer fails the quality gate, the in-process dispatch does not
redraft the same small model with the tier's retry budget (best-of-N exists for
a model that sometimes clears the bar; the lead rung is measured, and a miss is
the measurement). The exclusion moves the tier on to its own rung, which keeps
its full retry budget. The control runs the same ladder with the lead rung
unnamed: the old best-of-N redraft is back.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from omnimarket.handlers.routing.delegation_backend_resolution import (
    ModelResolvedDelegationBackend,
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

pytestmark = pytest.mark.unit

_LEAD = "local-small"
_RUNG = "local-heavy-reasoning"
_MODELS = {_LEAD: "small-local-model-q4", _RUNG: "Qwen3.8-27B"}
_GOOD = (
    "### ANSWER\nAccording to Smith (2020) and the theorem in section 3, the "
    "tradeoff is significant because the evidence shows X; therefore we conclude "
    "Y. See references [12] for the methodical analysis and the risk profile."
)
_REFUSAL = "I'm sorry, but I cannot help with that request. I refuse to answer."


def _backend(backend_id: str) -> ModelResolvedDelegationBackend:
    return ModelResolvedDelegationBackend(
        backend_id=backend_id,
        model_id=_MODELS[backend_id],
        endpoint_ref=f"https://{backend_id}.example/v1/chat/completions",
        tier="local",
        max_tokens=512,
        timeout_ms=30000,
    )


def _install(monkeypatch: pytest.MonkeyPatch, *, lead: frozenset[str]) -> None:
    monkeypatch.setattr(
        port_mod,
        "resolve_delegation_backend",
        lambda _task, *, backend_id=None: _backend(backend_id or _LEAD),
    )
    monkeypatch.setattr(
        port_mod, "first_eligible_tier", lambda _task, **_kwargs: "local"
    )
    monkeypatch.setattr(
        port_mod, "backend_id_for_tier", lambda _tier, _task, **_kw: _LEAD
    )
    monkeypatch.setattr(port_mod, "tier_for_backend", lambda _backend_id: "local")
    monkeypatch.setattr(
        port_mod,
        "next_eligible_tier",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        port_mod,
        "sibling_backend_available_in_tier",
        lambda _tier, _task, excluded: next(
            (ref for ref in (_LEAD, _RUNG) if ref not in excluded), None
        ),
    )
    monkeypatch.setattr(port_mod, "resolve_task_class_max_escalations", lambda _t: 2)
    monkeypatch.setattr(port_mod, "is_free_tier", lambda tier: tier == "local")
    monkeypatch.setattr(port_mod, "tier_max_retries", lambda _tier: 2)
    monkeypatch.setattr(port_mod, "lead_backend_refs", lambda: lead)


class _Effect:
    """Fails the lead rung's every draft and passes the tier rung's."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(
        self, request: ModelLlmDelegationCallRequest
    ) -> ModelLlmDelegationCallResult:
        by_model = {model: ref for ref, model in _MODELS.items()}
        ref = by_model[request.model_id]
        self.calls.append(ref)
        return ModelLlmDelegationCallResult(
            request_id=request.request_id,
            success=True,
            content=_GOOD if ref == _RUNG else _REFUSAL,
            tokens_in=11,
            tokens_out=22,
            latency_ms=5,
            actual_cost_usd=Decimal("0"),
            savings_usd=Decimal("0"),
        )


def _dispatch(port: LocalDelegationDispatchPort) -> dict[str, object]:
    return asyncio.run(
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
            tenant_id=None,
        )
    )


def _run(tmp_path: Path, effect: _Effect) -> dict[str, object]:
    return _dispatch(
        LocalDelegationDispatchPort(
            effect_handler=effect,
            evidence_db_path=tmp_path / "d.sqlite",
            effect_process_boundary=False,
        )
    )


def test_local_rung_route_a_lead_miss_goes_to_the_tier_rung_at_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(monkeypatch, lead=frozenset({_LEAD}))
    effect = _Effect()
    result = _run(tmp_path, effect)
    assert result["status"] == "completed"
    assert effect.calls == [_LEAD, _RUNG]


def test_local_rung_route_control_unnamed_lead_is_redrafted_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The failing control: the lead rung unnamed, the old best-of-N redraft."""
    _install(monkeypatch, lead=frozenset())
    effect = _Effect()
    result = _run(tmp_path, effect)
    assert result["status"] == "completed"
    assert effect.calls == [_LEAD, _LEAD, _LEAD, _RUNG]
