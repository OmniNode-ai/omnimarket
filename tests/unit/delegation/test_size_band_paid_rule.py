# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23180: a large task may climb to a paid model, a small one stays on free models.

The contract names the smallest measured size band that may reach a paid rung
(``escalation_policy.paid_from_size_band``, default ``L`` by the operator ruling
of 2026-11-08T13:46:43Z). A paid rung met by a smaller request is a skip with
cause ``size_band_below_paid``; with every free rung spent, the run goes back
to its caller. Fakes only: the ladder and the provider are stubs, so no model
is called and nothing is spent.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any, cast
from uuid import uuid4

import pytest
from omnibase_core.enums.enum_delegation_terminal_failure_cause import (
    EnumDelegationTerminalFailureCause,
)

from omnimarket.enums.enum_delegation_failure_class import EnumDelegationFailureClass
from omnimarket.handlers.delegation_paid_band import (
    paid_from_size_band_for,
    resolve_paid_from_size_band,
)
from omnimarket.handlers.routing.delegation_backend_resolution import (
    ModelResolvedDelegationBackend,
)
from omnimarket.inference.task_class_authority import load_task_class_authority
from omnimarket.models.delegation.model_escalation_transitions import (
    EnumEscalationCause,
    EnumTransitionKind,
    ModelTaskClassTransitions,
)
from omnimarket.models.delegation.model_paid_rung_decision import ModelPaidRungRequest
from omnimarket.models.delegation.model_size_band import EnumSizeBand
from omnimarket.models.delegation.wire.model_delegate_skill_request import (
    ModelDelegateSkillRequest,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.handler_delegate_skill import (
    HandlerDelegateSkill,
    ProtocolDelegationDispatchPort,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports import (
    port_local_delegation_dispatch as port_mod,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_local_delegation_dispatch import (
    LocalDelegationDispatchPort,
)
from omnimarket.nodes.node_delegation_escalation_decision_compute.handlers.handler_escalation_decision import (
    HandlerEscalationDecision,
)
from omnimarket.nodes.node_llm_delegation_call_effect import (
    ModelLlmDelegationCallRequest,
    ModelLlmDelegationCallResult,
)

pytestmark = pytest.mark.unit

_LADDER = ("local", "cheap_cloud")
_BACKEND = {"local": "local-coder", "cheap_cloud": "cloud-a"}
_TIER = {value: key for key, value in _BACKEND.items()}
_MODEL = {"local": "free-model", "cheap_cloud": "paid-model"}
_GOOD_ANSWER = (
    "### ANSWER\nAccording to Smith (2020) and the theorem in section 3, the "
    "tradeoff is significant because the evidence shows X; therefore we conclude "
    "Y. See references [12] for the methodical analysis and the risk profile this "
    "approach carries in practice."
)
_SMALL_PROMPT = "explain the tradeoff"
# Twelve list items: above the 10-unit edge of the default size_band_thresholds, so
# band L, while the text stays short enough for the free local rung.
_LARGE_PROMPT = "explain the tradeoff\n" + "".join(
    f"- compare design {n} on cost and risk\n" for n in range(12)
)
# Five list items: above the 3-unit S edge and within the 10-unit M edge.
_MEDIUM_PROMPT = "explain the tradeoff\n" + "".join(
    f"- compare design {n} on cost and risk\n" for n in range(5)
)


@pytest.fixture(autouse=True)
def _real_paid_rung_band(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bind the real resolver back: tests/conftest.py declares no band by default."""
    monkeypatch.setattr(
        port_mod, "resolve_paid_from_size_band", resolve_paid_from_size_band
    )


def _backend(tier: str) -> ModelResolvedDelegationBackend:
    return ModelResolvedDelegationBackend(
        backend_id=_BACKEND[tier],
        model_id=_MODEL[tier],
        endpoint_ref=f"https://{tier}-host.example:8443/v1/chat/completions",
        tier=tier,
        max_tokens=4096,
        timeout_ms=120000,
    )


def _install_ladder(monkeypatch: pytest.MonkeyPatch) -> None:
    """A two-rung ladder: a free local rung, then a paid cheap_cloud rung."""

    def resolve(task_type: str, *, backend_id: str | None = None) -> Any:
        return _backend(_LADDER[0] if backend_id is None else _TIER[backend_id])

    def next_tier(current: str, excluded: Any, **_: Any) -> str | None:
        later = _LADDER[_LADDER.index(current) + 1 :]
        return next((tier for tier in later if tier not in excluded), None)

    monkeypatch.setattr(port_mod, "resolve_delegation_backend", resolve)
    monkeypatch.setattr(port_mod, "next_eligible_tier", next_tier)
    monkeypatch.setattr(port_mod, "first_eligible_tier", lambda *_a, **_k: _LADDER[0])
    monkeypatch.setattr(
        port_mod, "backend_id_for_tier", lambda t, *_a, **_k: _BACKEND[t]
    )
    monkeypatch.setattr(port_mod, "tier_for_backend", lambda b: _TIER.get(b))
    monkeypatch.setattr(
        port_mod, "sibling_backend_available_in_tier", lambda *_a, **_k: None
    )
    monkeypatch.setattr(port_mod, "resolve_task_class_max_escalations", lambda _t: 1)
    monkeypatch.setattr(port_mod, "resolve_task_class_tier_order", lambda _t: _LADDER)
    monkeypatch.setattr(port_mod, "is_free_tier", lambda tier: tier == "local")
    monkeypatch.setattr(port_mod, "tier_max_retries", lambda _tier: 0)


class _Provider:
    """The free rung is unavailable; the paid rung answers. Records each call."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(
        self, request: ModelLlmDelegationCallRequest
    ) -> ModelLlmDelegationCallResult:
        self.calls.append(request.model_tier)
        if request.model_tier == "local":
            return ModelLlmDelegationCallResult(
                request_id=request.request_id,
                success=False,
                failure_class=EnumDelegationFailureClass.MODEL_UNAVAILABLE,
                error_message="provider answered HTTP 503: upstream overloaded",
                http_status=503,
            )
        return ModelLlmDelegationCallResult(
            request_id=request.request_id,
            success=True,
            content=_GOOD_ANSWER,
            tokens_in=11,
            tokens_out=22,
        )


def _port(tmp_path: Any, provider: _Provider) -> LocalDelegationDispatchPort:
    return LocalDelegationDispatchPort(
        effect_handler=provider,
        evidence_db_path=tmp_path / "delegation.sqlite",
        effect_process_boundary=False,
    )


def _dispatch(port: LocalDelegationDispatchPort, prompt: str) -> dict[str, Any]:
    result: dict[str, Any] = asyncio.run(
        port.dispatch(
            prompt=prompt,
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
    return result


def test_the_contract_declares_large_only_and_the_cause_is_a_skip() -> None:
    """The ruling is in the contract: L is the default, and the cause is a declared skip."""
    authority = load_task_class_authority()
    assert paid_from_size_band_for(authority, "research") is EnumSizeBand.L
    assert resolve_paid_from_size_band("research") is EnumSizeBand.L
    for name, entry in authority.task_classes.items():
        assert entry.transitions is not None, name
        assert (
            entry.transitions.kind_of(EnumEscalationCause.SIZE_BAND_BELOW_PAID)
            is EnumTransitionKind.SKIP
        ), name


@pytest.mark.parametrize(
    "prompt",
    [
        pytest.param(_SMALL_PROMPT, id="small_stays_free"),
        pytest.param(_MEDIUM_PROMPT, id="medium_stays_free"),
    ],
)
def test_a_small_request_skips_the_paid_rung_and_goes_back_to_its_caller(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, prompt: str
) -> None:
    """AC1, S half (and M, under the large-only ruling): the paid rung is a named skip."""
    _install_ladder(monkeypatch)
    provider = _Provider()
    port = _port(tmp_path, provider)

    result = _dispatch(port, prompt)

    assert provider.calls == ["local"]
    assert result["status"] == "failed"
    skips = result["skipped_rungs"]
    assert [(s["tier"], s["rule"]) for s in skips] == [
        ("cheap_cloud", EnumEscalationCause.SIZE_BAND_BELOW_PAID.value)
    ]
    assert "below L" in str(skips[0]["reason"])

    terminal = asyncio.run(
        HandlerDelegateSkill(
            dispatch_port=cast(ProtocolDelegationDispatchPort, _ReplayPort(result))
        ).handle(
            ModelDelegateSkillRequest(
                prompt=prompt,
                task_type="research",
                source="external-client",
                correlation_id=uuid.uuid4(),
            )
        )
    )
    assert terminal is not None
    assert (
        terminal.terminal_failure_cause
        is EnumDelegationTerminalFailureCause.RETURNED_TO_CALLER
    )


class _ReplayPort:
    """Hands the handler the result the real port produced for the small request."""

    def __init__(self, result: dict[str, Any]) -> None:
        self._result = result

    async def dispatch(self, **_kwargs: object) -> dict[str, Any]:
        return self._result


def test_a_large_request_is_accepted_on_the_paid_rung(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC1, L half: the same rungs, a large request, answered on the paid rung."""
    _install_ladder(monkeypatch)
    provider = _Provider()

    result = _dispatch(_port(tmp_path, provider), _LARGE_PROMPT)

    assert provider.calls == ["local", "cheap_cloud"]
    assert result["status"] == "completed"
    assert not result.get("skipped_rungs")
    assert result["attempts"][-1]["tier"] == "cheap_cloud"


def test_control_without_the_rule_a_small_request_reaches_the_paid_rung(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Failing control: with no band declared the paid rung is reached by a small request."""
    _install_ladder(monkeypatch)
    monkeypatch.setattr(port_mod, "resolve_paid_from_size_band", lambda _t: None)
    provider = _Provider()

    result = _dispatch(_port(tmp_path, provider), _SMALL_PROMPT)

    assert provider.calls == ["local", "cheap_cloud"]
    assert result["status"] == "completed"


def test_a_class_override_lowers_the_band(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A class that declares paid_from_size_band S lets a small request climb."""
    _install_ladder(monkeypatch)
    monkeypatch.setattr(
        port_mod, "resolve_paid_from_size_band", lambda _t: EnumSizeBand.S
    )
    provider = _Provider()

    result = _dispatch(_port(tmp_path, provider), _SMALL_PROMPT)

    assert provider.calls == ["local", "cheap_cloud"]
    assert result["status"] == "completed"


_TRANSITIONS = ModelTaskClassTransitions.model_validate(
    {
        "retry_on": ["transient_provider_error", "quality_gate_refused"],
        "move_on": [
            "retry_budget_spent",
            "rate_limited",
            "rung_timeout",
            "rubric_failed",
        ],
        "skip_on": [
            c.value
            for c in EnumEscalationCause
            if c
            not in {
                EnumEscalationCause.TRANSIENT_PROVIDER_ERROR,
                EnumEscalationCause.QUALITY_GATE_REFUSED,
                EnumEscalationCause.RETRY_BUDGET_SPENT,
                EnumEscalationCause.RATE_LIMITED,
                EnumEscalationCause.RUNG_TIMEOUT,
                EnumEscalationCause.RUBRIC_FAILED,
                EnumEscalationCause.PINNED_BACKEND_FAILED,
                EnumEscalationCause.QUALITY_VETO,
                EnumEscalationCause.ESCALATION_LIMIT,
                EnumEscalationCause.LADDER_EXHAUSTED,
                EnumEscalationCause.NON_RETRYABLE_FAILURE,
            }
        ],
        "terminate_on": [
            "pinned_backend_failed",
            "quality_veto",
            "escalation_limit",
            "ladder_exhausted",
            "non_retryable_failure",
        ],
    }
)


@pytest.mark.parametrize(
    ("band", "paid_from", "paid", "allowed"),
    [
        (EnumSizeBand.S, EnumSizeBand.L, True, False),
        (EnumSizeBand.M, EnumSizeBand.L, True, False),
        (EnumSizeBand.L, EnumSizeBand.L, True, True),
        (EnumSizeBand.M, EnumSizeBand.M, True, True),
        (EnumSizeBand.S, EnumSizeBand.L, False, True),
        (EnumSizeBand.S, None, True, True),
        (None, EnumSizeBand.L, True, False),
        (None, None, True, True),
    ],
)
def test_the_decision_node_skips_a_paid_rung_below_the_band(
    band: EnumSizeBand | None,
    paid_from: EnumSizeBand | None,
    paid: bool,
    allowed: bool,
) -> None:
    verdict = HandlerEscalationDecision().decide_paid_rung(
        ModelPaidRungRequest(
            rung_is_paid=paid,
            size_band=band,
            paid_from_size_band=paid_from,
            transitions=_TRANSITIONS,
        )
    )
    assert verdict.allowed is allowed
    if allowed:
        assert verdict.transition is None
    else:
        assert verdict.transition is not None
        assert verdict.transition.kind is EnumTransitionKind.SKIP
        assert verdict.transition.cause is EnumEscalationCause.SIZE_BAND_BELOW_PAID
        assert verdict.reason is not None


def test_a_class_escalation_policy_value_overrides_the_default_and_a_bad_one_is_refused() -> (
    None
):
    """The per-class value wins over the top-level default; a non-band value is a contract error."""
    authority = load_task_class_authority()
    entry = authority.task_classes["research"]
    assert entry.escalation_policy is not None
    policy = entry.escalation_policy.model_copy(update={"paid_from_size_band": "M"})
    declared = authority.model_copy(
        update={
            "task_classes": {
                **authority.task_classes,
                "research": entry.model_copy(update={"escalation_policy": policy}),
            }
        }
    )
    assert paid_from_size_band_for(declared, "research") is EnumSizeBand.M

    broken = declared.model_copy(update={"paid_from_size_band": "XL"})
    other = next(n for n in authority.task_classes if n != "research")
    with pytest.raises(ValueError, match="paid_from_size_band must be one of"):
        paid_from_size_band_for(broken, other)
