# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A delegation check asserts the chain's pick served the run and that no request names a model (OMN-26043).

The chain audit found the gateway checks read only that a receipt names a route, a provider and a model: a run served out of
order, or by a provider the chain did not pick, read green. Each check that runs a delegation now carries the clause
``serving_tier_is_chain_pick`` (the receipt's tier is the first available tier of the chain) and ``no_model_named_in_request``;
the own-key check C29 carries ``only_chosen_provider_answered``. Known good passes; known bad fails.
"""

from __future__ import annotations

from typing import Any

import pytest
from _gateway_fake import FakeClock, FakeGateway

from omnimarket.handlers.routing.delegation_check_responder_chain import (
    ModelResponderReading,
)
from omnimarket.models.overlay_documents.model_delegation_check_responders_overlay import (
    ModelDelegationCheckResponder,
)
from omnimarket.nodes.node_delegation_check_probe_effect.handlers.adapter_delegation_check import (
    AbsentCustomerMachine,
    InMemoryCheckPublisher,
    InMemoryResponderReader,
)
from omnimarket.nodes.node_delegation_check_probe_effect.handlers.gateway_checks import (
    GatewayRunSettings,
)
from omnimarket.nodes.node_delegation_check_probe_effect.handlers.grade_delegation_checks import (
    grade_no_model_named,
    grade_own_key_provider,
    grade_serving_tier,
)
from omnimarket.nodes.node_delegation_check_probe_effect.handlers.handler_delegation_check_probe import (
    HandlerDelegationCheckProbe,
    contract_topics,
)
from omnimarket.nodes.node_delegation_check_probe_effect.models.model_delegation_check_probe import (
    CheckId,
    EnumCheckVerdict,
    ModelDelegationCheckRequest,
)
from omnimarket.nodes.node_delegation_check_probe_effect.models.model_gateway_check import (
    ModelGatewayObservation,
)

BASE = "http://127.0.0.12:8000"
LOCAL = ModelDelegationCheckResponder(tier="local", base_url=BASE)
OPENROUTER = ModelDelegationCheckResponder(tier="openrouter")
CLAUDE = ModelDelegationCheckResponder(tier="claude_api")
CHAIN = (LOCAL, OPENROUTER, CLAUDE)
_CLOCK = FakeClock()
SERVING = "serving_tier_is_chain_pick"


def _run(
    check: CheckId,
    *,
    local_answers: bool = True,
    receipt_provider: str | None = None,
    chosen_provider: str | None = "local",
    gateway: FakeGateway | None = None,
) -> Any:
    gateway = gateway or FakeGateway()
    gateway.chosen_provider = chosen_provider
    if receipt_provider is not None:
        original = gateway.call

        def call(method: str, path: str, **kw: Any) -> ModelGatewayObservation:
            observed = original(method, path, **kw)
            if (
                "/receipt" in path
                and observed.status == 200
                and isinstance(observed.body, dict)
                and observed.body.get("provider") is not None
            ):
                return ModelGatewayObservation(
                    status=200, body={**observed.body, "provider": receipt_provider}
                )
            return observed

        gateway.call = call  # type: ignore[method-assign]
    reading = ModelResponderReading(
        base_url=BASE,
        answered=local_answers,
        served_model="m" if local_answers else None,
        n_ctx=4096 if local_answers else None,
    )
    handler = HandlerDelegationCheckProbe(
        responders=CHAIN,
        reader=InMemoryResponderReader({BASE: reading}),
        machine=AbsentCustomerMachine("none"),
        publisher=InMemoryCheckPublisher(),
        topic=contract_topics()[0],
        gateway=gateway,
        gateway_settings=GatewayRunSettings(run_budget_s=5.0, poll_interval_s=0.0),
        sleep=_CLOCK.sleep,
        monotonic=_CLOCK.monotonic,
    )
    return handler.handle(
        ModelDelegationCheckRequest(correlation_id="t", check_id=check)
    )


def _failed(result: Any) -> set[str]:
    return {c.name for c in result.clauses if not c.ok}


def _named(result: Any) -> dict[str, bool]:
    return {c.name: c.ok for c in result.clauses}


@pytest.mark.parametrize("check", ["C15", "C16", "C28", "C30"])
def test_known_good_local_pick_served_by_local_passes(check: CheckId) -> None:
    result = _run(check)
    assert _named(result)[SERVING] is True
    assert _named(result)["no_model_named_in_request"] is True
    assert result.verdict is EnumCheckVerdict.PASS


@pytest.mark.parametrize("check", ["C15", "C16", "C28", "C30"])
def test_known_bad_run_served_by_a_tier_the_chain_did_not_pick_fails(
    check: CheckId,
) -> None:
    # A local responder answers, so the chain picks local; the receipt says OpenRouter served: out of order.
    result = _run(check, receipt_provider="openrouter")
    assert result.verdict is EnumCheckVerdict.FAIL
    assert _failed(result) == {SERVING}


def test_known_bad_run_served_by_local_when_the_chain_advanced_fails() -> None:
    # No local responder answers: the chain advances to OpenRouter. A receipt that still names local was not the chain's pick.
    result = _run("C15", local_answers=False)
    assert result.verdict is EnumCheckVerdict.FAIL
    assert _failed(result) == {SERVING}


def test_known_good_advanced_chain_served_by_openrouter_passes() -> None:
    result = _run("C15", local_answers=False, receipt_provider="openrouter")
    assert _named(result)[SERVING] is True
    assert result.verdict is EnumCheckVerdict.PASS


def test_known_bad_receipt_provider_outside_the_chain_fails() -> None:
    result = _run("C16", receipt_provider="some-pinned-backend")
    assert _failed(result) == {SERVING}


def test_c13_and_c14_gateway_legs_carry_the_serving_clause() -> None:
    for check in ("C13", "C14"):
        good = _run(check)
        assert _named(good)[SERVING] is True
        bad = _run(check, receipt_provider="claude_api")
        assert SERVING in _failed(bad)


def test_the_clause_reads_the_declared_chain_pick() -> None:
    assert grade_serving_tier("local", "local").ok
    assert not grade_serving_tier("openrouter", "local").ok
    assert not grade_serving_tier("local", None).ok
    assert not grade_serving_tier(None, "local").ok
    assert not grade_serving_tier("claude_api", "openrouter").ok


def test_a_request_naming_a_model_backend_or_provider_fails_the_clause() -> None:
    assert grade_no_model_named([{"prompt": "x", "task_type": "test"}]).ok
    for field in ("model", "backend_id", "provider", "model_id", "base_url"):
        clause = grade_no_model_named([{"prompt": "x", field: "v"}])
        assert not clause.ok
        assert field in clause.detail
    assert not grade_no_model_named([]).ok


def test_the_requests_a_check_sends_name_no_model() -> None:
    gateway = FakeGateway()
    seen: list[dict[str, Any]] = []
    original = gateway.call

    def call(method: str, path: str, **kw: Any) -> ModelGatewayObservation:
        if method == "POST" and kw.get("body"):
            seen.append(kw["body"]["payload"])
        return original(method, path, **kw)

    gateway.call = call  # type: ignore[method-assign]
    _run("C15", gateway=gateway)
    assert seen
    assert all(
        f not in payload
        for payload in seen
        for f in ("model", "backend_id", "provider")
    )


def test_own_key_known_good_the_chosen_provider_answered_passes() -> None:
    result = _run("C29", chosen_provider="local")
    assert _named(result)["only_chosen_provider_answered"] is True
    assert result.verdict is EnumCheckVerdict.PASS


def test_own_key_known_bad_another_provider_answered_fails() -> None:
    result = _run("C29", chosen_provider="claude_api")
    assert result.verdict is EnumCheckVerdict.FAIL
    assert _failed(result) == {"only_chosen_provider_answered"}


def test_own_key_with_no_chosen_provider_is_precondition_absent_never_green() -> None:
    result = _run("C29", chosen_provider=None)
    assert result.verdict is EnumCheckVerdict.PRECONDITION_ABSENT
    assert any(p.name == "chosen_provider" for p in result.preconditions)


def test_own_key_clause_compares_the_receipt_provider() -> None:
    assert grade_own_key_provider("claude_api", "claude_api").ok
    assert not grade_own_key_provider("openrouter", "claude_api").ok
    assert not grade_own_key_provider(None, "claude_api").ok
    assert not grade_own_key_provider("local", None).ok
