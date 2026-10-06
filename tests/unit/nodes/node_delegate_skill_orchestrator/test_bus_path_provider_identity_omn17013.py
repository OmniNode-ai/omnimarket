# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-17013 (DR-02) — the bus-path receipt must bind a provider IDENTITY.

``HandlerDelegateSkill`` builds the receipt's ``provider`` field from the dict the
injected dispatch port returns. On the DEPLOYED BUS path that dict is the parsed
terminal event — an ``omnibase_core`` ``ModelDelegationCompleted`` /
``ModelDelegationFailed`` payload, dumped to JSON by the producing orchestrator and
flattened back by ``RuntimeDelegationDispatchPort`` (see
``port_runtime_delegation_dispatch._flatten_terminal_payload``). That model carries a
declared ``provider`` identity and a raw ``endpoint_url``. It has never carried a
``delegated_to`` field — that key exists only on the LOCAL in-process port's
hand-built dicts, which is exactly why the defect is invisible to ``onex delegate``
CLI testing and shows up only on the deployed lane.

The pre-fix handler read ``result.get("delegated_to") or result.get("endpoint_url")``.
On the bus path the first key is always absent, so every bus receipt stamped its
``provider`` with the raw endpoint URL — a LAN address for local rungs, not a stable
provider identity — while the real ``provider`` sat unread in the same payload. Cost
attribution and provenance audits built on those receipts read an address where an
identity belongs.

``provider`` was added to the wire model by OMN-18079 (omnibase_core#1675,
``model_delegation_result.py:54``), which is what makes the correct binding
available; nothing had been rewired to read it.

These tests go RED against the pre-fix handler (``provider`` == the endpoint URL) and
GREEN once it reads the declared identity. The payload is constructed from the REAL
core model rather than a hand-written dict, so a future field rename breaks this test
instead of silently passing.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from omnibase_core.models.delegation.wire.model_delegation_completed import (
    ModelDelegationCompleted,
)

from omnimarket.enums.enum_delegation_acceptance import (
    EnumDelegationAcceptanceDecision,
)
from omnimarket.models.delegation.wire.model_delegate_skill_request import (
    ModelDelegateSkillRequest,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.handler_delegate_skill import (
    HandlerDelegateSkill,
)

# The raw endpoint the rung was actually posted to. This is the value the pre-fix
# handler stamped as the receipt's provider — the defect under test. On the live
# lane this is a LAN address for local rungs; a reserved .invalid host stands in
# here so the fixture carries no real address of its own.
_ENDPOINT_URL = "http://local-runtime.invalid:1234/v1/chat/completions"
# The declared provider identity for the selected route, as the routing authority
# resolved it. This is what the receipt must carry.
_PROVIDER_IDENTITY = "lmstudio-local"
# The selected backend route. The wire model refuses a provider without one.
_ROUTE = "local-tier-1"


def _bus_terminal_result(
    correlation_id: Any,
    *,
    provider: str | None,
    escalation_history: tuple[dict[str, object], ...] = (),
) -> dict[str, object]:
    """Build the exact dict the BUS dispatch port hands the handler.

    The runtime port returns ``{"status", "correlation_id"}`` updated with the
    flattened terminal payload, and that payload is the JSON dump of the core
    terminal model. Dumping the real model here is what makes this a bus-path
    fixture rather than an assertion about a dict this test invented: when
    ``provider`` is None the model's ``exclude_if`` predicate DROPS the key
    entirely, which is the live absent-key shape the handler must survive.
    """
    terminal = ModelDelegationCompleted(
        correlation_id=correlation_id,
        task_type="test",
        model_used="qwen-coder",
        endpoint_url=_ENDPOINT_URL,
        # The model validates route and provider as a PAIR — one without the
        # other is refused — so the absent case must drop both, which is also
        # exactly how an unrouted terminal is published.
        route=_ROUTE if provider is not None else None,
        provider=provider,
        content="bus-path provider identity proof",
        quality_passed=True,
        quality_score=0.95,
        latency_ms=1200,
        fallback_to_claude=False,
        escalation_history=escalation_history,
        attempts_count=max(len(escalation_history), 1),
        escalation_count=max(len(escalation_history) - 1, 0),
    )
    payload = terminal.model_dump(mode="json")
    return {"status": "completed", **payload}


class _BusTerminalDispatchPort:
    """Stub port returning a bus-shaped terminal, with no network and no broker.

    Declares the full ``ProtocolDelegationDispatchPort`` keyword surface: the handler
    always passes every one of these, and a stub that rejects an unexpected keyword
    surfaces as a plausible ``status="failed"`` receipt rather than a loud TypeError,
    which would make this test pass for the wrong reason.
    """

    def __init__(
        self,
        *,
        provider: str | None,
        escalation_history: tuple[dict[str, object], ...] = (),
    ) -> None:
        self._provider = provider
        self._escalation_history = escalation_history

    async def dispatch(
        self,
        *,
        prompt: str,
        task_type: str,
        correlation_id: Any,
        max_tokens: int | None,
        source_file_path: str | None,
        source_session_id: str | None,
        wait: bool,
        # OMN-15504: the handler always threads its resolved execution budget.
        execution_timeout_seconds: int,
        terminal_delivery_margin_seconds: int,
        quality_contract_mode: str,
        acceptance_criteria: tuple[str, ...],
        tenant_id: str | None,
        backend_id: str | None = None,
        response_contract: dict[str, object] | None = None,
        system_prompt: str | None = None,
        temperature: float | None = None,
        response_format: dict[str, object] | None = None,
        provenance: object | None = None,
    ) -> dict[str, object]:
        return _bus_terminal_result(
            correlation_id,
            provider=self._provider,
            escalation_history=self._escalation_history,
        )


async def _receipt(
    *,
    provider: str | None,
    escalation_history: tuple[dict[str, object], ...] = (),
) -> Any:
    handler = HandlerDelegateSkill(
        dispatch_port=_BusTerminalDispatchPort(
            provider=provider, escalation_history=escalation_history
        )
    )
    response = await handler.handle(
        ModelDelegateSkillRequest(
            prompt="Prove the bus receipt binds a provider identity",
            task_type="test",
            source="claude-code",
            correlation_id=uuid4(),
        )
    )
    # The handler converts ANY dispatch exception into a status="failed" receipt
    # whose provider is "". Without this guard a broken fixture (a model field
    # renamed, a keyword the stub fails to declare) would satisfy every
    # "provider is not a URL" assertion below while proving nothing — measured,
    # not hypothesised: it is how the first RED run of this file reported one
    # passing test before the fixture was complete.
    assert response.status == "completed", (
        "fixture precondition: dispatch must have succeeded, otherwise the "
        f"provider assertions are vacuous; got status={response.status!r} "
        f"error={response.error_message!r}"
    )
    return response


@pytest.mark.unit
async def test_bus_receipt_provider_is_the_declared_identity_not_the_endpoint_url() -> (
    None
):
    """The load-bearing assertion: identity in, not the address it was reached at."""
    response = await _receipt(provider=_PROVIDER_IDENTITY)

    assert response.provider == _PROVIDER_IDENTITY, (
        "the bus receipt must bind the declared provider identity from the terminal "
        f"payload; got {response.provider!r}"
    )


@pytest.mark.unit
async def test_bus_receipt_provider_is_never_a_raw_endpoint_url() -> None:
    """Pin the defect shape itself, so a future regression is named, not inferred.

    Stated separately from the assertion above because the DoD forbids the URL
    independently of what the right identity happens to be: a later change that
    stamped some OTHER address here would still be the same class of defect.
    """
    response = await _receipt(provider=_PROVIDER_IDENTITY)

    assert response.provider != _ENDPOINT_URL
    assert "://" not in response.provider, (
        "a receipt provider containing a URL scheme is an address, not an identity; "
        f"got {response.provider!r}"
    )


@pytest.mark.unit
async def test_absent_provider_yields_explicit_empty_not_a_url() -> None:
    """An unrouted terminal drops the key entirely; the receipt must not substitute.

    ``provider`` carries ``exclude_if=lambda value: value is None``, so a pre-route
    failure publishes a payload with NO ``provider`` key at all. Falling back to the
    endpoint URL there is the precise behaviour DR-02 rejects: an explicit absence is
    honest, an address stamped in its place is wrong data that downstream cost
    attribution cannot tell apart from a real identity.
    """
    payload = _bus_terminal_result(uuid4(), provider=None)
    assert "provider" not in payload, (
        "fixture precondition: a None provider must be absent from the wire payload, "
        "not null — otherwise this test does not exercise the absent-key path"
    )

    response = await _receipt(provider=None)

    assert response.provider == ""
    assert response.provider != _ENDPOINT_URL


# OMN-17013 DoD item 4: one receipt, built from the real core wire terminal on the
# bus path, asserting all three bindings together. The rung that answered is the
# SECOND rung: a first rung abandoned (``climb``) and a second accepted, which is
# the shape the live lane writes (the deployed delegation_events rows carry exactly
# this ladder). A receipt that stamped the first rung, or defaulted the gate to
# False, or left the manifest version at its 0 default, fails here by name.
_LADDER: tuple[dict[str, object], ...] = (
    {
        "tier": "local",
        "backend_ref": "local-omnipc2-chat",
        "model_used": "qwen-coder",
        "acceptance_decision": "climb",
        "acceptance_reason": "provider_call_failed",
        "failure_reasons": ["provider call failed"],
    },
    {
        "tier": "local",
        "backend_ref": "local-heavy-reasoning",
        "model_used": "qwen-coder",
        "acceptance_decision": "accept",
        "acceptance_reason": "quality_bar_met",
        "quality_score": 0.95,
    },
)


@pytest.mark.unit
async def test_bus_receipt_binds_provider_accepted_attempt_gate_and_manifest_version() -> (
    None
):
    from omnimarket.pricing import get_manifest_version_int

    response = await _receipt(provider=_PROVIDER_IDENTITY, escalation_history=_LADDER)

    # (1) a provider identity, never the endpoint address.
    assert response.provider == _PROVIDER_IDENTITY
    assert "://" not in response.provider

    # (2) the ladder carries the accepted terminal attempt, and the receipt's gate
    # is the real outcome (True here) rather than a hardcoded False.
    assert len(response.attempts) == 2
    terminal_attempt = response.attempts[-1]
    assert (
        terminal_attempt.acceptance_decision is EnumDelegationAcceptanceDecision.ACCEPT
    )
    assert terminal_attempt.quality_gate_passed is True
    assert terminal_attempt.backend_id == "local-heavy-reasoning"
    assert response.attempts[0].quality_gate_passed is False
    assert response.quality_gate_passed is True

    # (3) a real manifest version from the pricing manifest, not the field's 0 default.
    assert response.pricing_manifest_version > 0
    assert response.pricing_manifest_version == get_manifest_version_int()


@pytest.mark.unit
@pytest.mark.parametrize("outcome", ["completed", "failed_routed", "failed_unrouted"])
async def test_v2_bus_terminal_identity_reaches_receipt(outcome: str) -> None:
    from datetime import UTC, datetime

    from omnibase_core.models.delegation.wire.model_delegation_terminal_v2 import (
        ModelDelegationTerminalCompletedV2,
        ModelDelegationTerminalFailedRoutedV2,
        ModelDelegationTerminalFailedUnroutedV2,
    )
    from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
    from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory
    from omnibase_infra.event_bus.models.model_event_message import ModelEventMessage

    from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_runtime_delegation_dispatch import (
        RuntimeDelegationDispatchPort,
        load_runtime_delegation_dispatch_config,
    )
    from omnimarket.nodes.node_delegation_orchestrator.contract_topics import (
        TOPIC_ID_DELEGATION_COMPLETED_V2,
        TOPIC_ID_DELEGATION_FAILED_ROUTED_V2,
        TOPIC_ID_DELEGATION_FAILED_UNROUTED_V2,
    )
    from omnimarket.pricing import get_manifest_version_int

    correlation_id = uuid4()
    # Distinct versions falsify a receipt reconstructed from the reader's manifest.
    wire_version = get_manifest_version_int() + 1
    common: dict[str, Any] = {
        "correlation_id": correlation_id,
        "task_type": "test",
        "model_used": "qwen-coder",
        "endpoint_url": _ENDPOINT_URL,
        "content": "bus receipt proof",
        "latency_ms": 12,
        "prompt_tokens": 3,
        "completion_tokens": 2,
        "total_tokens": 5,
        "fallback_to_claude": False,
        "failure_reason": "",
        "tokens_to_compliance": 0,
        "compliance_attempts": 1,
        "escalation_count": 1,
        "escalation_history": _LADDER,
        "routing_tiers_hash": "test-routing",
        "escalation_config_hash": "test-escalation",
        "attempts_count": 2,
        "cumulative_attempt_cost": 0.0,
        "cumulative_input_tokens": 3,
        "cumulative_output_tokens": 2,
        "final_attempt_cost": 0.0,
        "context_pack_hash": "",
        "cost_tier_name": "local",
        "tenant_id": "test-tenant",
        "terminal_outcome": "completed" if outcome == "completed" else "failed",
    }
    routed: dict[str, Any] = {
        "routing_disposition": "routed",
        "backend_ref": "local-heavy-reasoning",
        "pricing_manifest_version": wire_version,
        "quality_bar_evaluation": {
            "quality_score": 0.95,
            "required_quality_bar": 0.8,
            "score_vs_required_bar": "at_or_above_bar",
        },
        "failed_acceptance_criteria": (),
    }
    config = load_runtime_delegation_dispatch_config().model_copy(
        update={"wait_timeout_seconds": 1}
    )
    if outcome == "completed":
        terminal = ModelDelegationTerminalCompletedV2(
            **common, **routed, quality_passed=True
        )
        topic = TOPIC_ID_DELEGATION_COMPLETED_V2
    elif outcome == "failed_routed":
        common["escalation_history"] = _LADDER[:1]
        routed["quality_bar_evaluation"] = {
            "quality_score": 0.2,
            "required_quality_bar": 0.8,
            "score_vs_required_bar": "below_bar",
        }
        terminal = ModelDelegationTerminalFailedRoutedV2(
            **common,
            **routed,
            quality_passed=False,
            terminal_failure_reason="quality gate refused",
            routed_failure_cause={"kind": "quality_gate_rejection"},
        )
        topic = TOPIC_ID_DELEGATION_FAILED_ROUTED_V2
    else:
        common["escalation_history"] = ()
        terminal = ModelDelegationTerminalFailedUnroutedV2(
            **common,
            routing_disposition="unrouted",
            unrouted_reason="no_eligible_backend",
            terminal_failure_reason="no eligible backend",
        )
        topic = TOPIC_ID_DELEGATION_FAILED_UNROUTED_V2

    bus = EventBusInmemory(environment="test", group="receipt-v2")
    await bus.start()

    async def on_command(message: ModelEventMessage) -> None:
        envelope = ModelEventEnvelope[type(terminal)](
            payload=terminal,
            correlation_id=correlation_id,
            envelope_timestamp=datetime.now(UTC),
            event_type=topic,
            source_tool="receipt-v2-test",
        )
        await bus.publish(topic, None, envelope.model_dump_json().encode(), None)

    try:
        await bus.subscribe(
            config.topics.command, group_id="receipt-v2-producer", on_message=on_command
        )
        handler = HandlerDelegateSkill(
            dispatch_port=RuntimeDelegationDispatchPort(event_bus=bus, config=config)
        )
        response = await handler.handle(
            ModelDelegateSkillRequest(
                prompt="Prove v2 bus receipt identity",
                task_type="test",
                source="claude-code",
                correlation_id=correlation_id,
                tenant_id="test-tenant",
            )
        )
    finally:
        await bus.close()

    assert response.status == ("completed" if outcome == "completed" else "failed")
    assert response.provider == (
        "" if outcome == "failed_unrouted" else "local-heavy-reasoning"
    )
    assert response.pricing_manifest_version == (
        0 if outcome == "failed_unrouted" else wire_version
    )
    assert response.quality_gate_passed is (outcome == "completed")
    if outcome == "completed":
        assert len(response.attempts) == 2
        assert response.attempts[-1].backend_id == response.provider
        assert (
            response.attempts[-1].acceptance_decision
            is EnumDelegationAcceptanceDecision.ACCEPT
        )
        assert response.attempts[-1].quality_gate_passed is True
        assert response.quality_score == 0.95
    else:
        assert response.error_message == terminal.terminal_failure_reason
