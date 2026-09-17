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

from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.handler_delegate_skill import (
    HandlerDelegateSkill,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.models.model_delegate_skill_request import (
    ModelDelegateSkillRequest,
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

    def __init__(self, *, provider: str | None) -> None:
        self._provider = provider

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
        return _bus_terminal_result(correlation_id, provider=self._provider)


async def _receipt(*, provider: str | None) -> Any:
    handler = HandlerDelegateSkill(
        dispatch_port=_BusTerminalDispatchPort(provider=provider)
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
