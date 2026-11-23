# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19432: a refused house key moves the call to the next declared key.

Fakes only, no network. Key values never appear: keys are named by reference.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from omnimarket.enums.enum_delegation_failure_class import (
    EnumDelegationFailureClass as Failure,
)
from omnimarket.events.llm_delegation_call import ModelLlmDelegationCallRequest
from omnimarket.nodes.node_llm_delegation_call_effect.handlers import (
    handler_provider_key_failover as failover_module,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_llm_delegation_call import (
    HandlerLlmDelegationCall,
)
from omnimarket.nodes.node_llm_delegation_call_effect.models.model_llm_delegation_call_result import (
    ModelLlmDelegationCallResult,
)

pytestmark = pytest.mark.unit

URL = "https://provider.invalid/v1/chat/completions"
KEY_A = "llm.house.first.api_key"
KEY_B = "llm.house.second.api_key"
CUSTOMER_REF = f"cred_acme_anthropic_{'a' * 32}"


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Pool:
    def __init__(self, *refs: str) -> None:
        self.refs = refs

    def refs_for(self, *, provider: str, endpoint_url: str) -> tuple[str, ...]:
        return self.refs


class FakeProvider:
    """Answers per key NAME: a status code, or 200 for success."""

    def __init__(self, answers: dict[str, int]) -> None:
        self.answers = answers
        self.calls: list[str | None] = []

    def post(
        self, request: ModelLlmDelegationCallRequest
    ) -> ModelLlmDelegationCallResult:
        self.calls.append(request.secret_ref)
        status = self.answers.get(request.secret_ref or "", 200)
        if status == 200:
            return ModelLlmDelegationCallResult(
                request_id=request.request_id,
                success=True,
                content="ok",
                tokens_in=1,
                tokens_out=1,
                http_status=200,
                secret_ref=request.secret_ref,
            )
        return ModelLlmDelegationCallResult(
            request_id=request.request_id,
            success=False,
            failure_class=Failure.PROVIDER_AUTH_FAILED,
            error_message=f"provider_auth_failed: {status}",
            http_status=status,
            secret_ref=request.secret_ref,
        )


def _request(secret_ref: str | None) -> ModelLlmDelegationCallRequest:
    return ModelLlmDelegationCallRequest(
        request_id=str(uuid4()),
        correlation_id=str(uuid4()),
        causation_id=str(uuid4()),
        model_id="claude-sonnet-5-5",
        endpoint_ref=URL,
        prompt="synthetic",
        prompt_hash="0" * 64,
        timeout_seconds=5,
        provider="anthropic",
        secret_ref=secret_ref,
        api_key_env=None,
    )


def _handler(
    monkeypatch: pytest.MonkeyPatch,
    provider: FakeProvider,
    pool: Pool,
    clock: Clock,
    cooldown: float = 300.0,
) -> HandlerLlmDelegationCall:
    handler = HandlerLlmDelegationCall()
    monkeypatch.setattr(
        handler,
        "_post_one_call",
        lambda request, *_args, **_kwargs: provider.post(request),
        raising=False,
    )
    monkeypatch.setattr(
        handler,
        "key_failover",
        failover_module.ProviderKeyFailover(
            pool=pool, cooldown_seconds=cooldown, clock=clock
        ),
        raising=False,
    )
    return handler


def _run(handler: HandlerLlmDelegationCall, ref: str | None) -> Any:
    return handler._execute_call_attempts(_request(ref), URL, None)


def test_first_key_401_then_second_key_answers_and_receipt_names_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = FakeProvider({KEY_A: 401})
    handler = _handler(monkeypatch, provider, Pool(KEY_A, KEY_B), Clock())

    result = _run(handler, KEY_A)

    assert result.success
    assert provider.calls == [KEY_A, KEY_B]
    assert result.secret_ref == KEY_B
    names = [(a.key_name, a.failure_class) for a in result.key_failover_attempts]
    assert names == [(KEY_A, Failure.PROVIDER_AUTH_FAILED), (KEY_B, None)]


def test_all_keys_fail_ends_provider_auth_failed_for_the_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = FakeProvider({KEY_A: 401, KEY_B: 403})
    handler = _handler(monkeypatch, provider, Pool(KEY_A, KEY_B), Clock())

    result = _run(handler, KEY_A)

    assert not result.success
    assert result.failure_class is Failure.PROVIDER_AUTH_FAILED
    assert provider.calls == [KEY_A, KEY_B]
    assert [a.key_name for a in result.key_failover_attempts] == [KEY_A, KEY_B]
    # Held out: the next call reaches no provider and ends with the same typed
    # failure, so the chain moves on to the next model without a retry storm.
    again = _run(handler, KEY_A)
    assert again.failure_class is Failure.PROVIDER_AUTH_FAILED
    assert provider.calls == [KEY_A, KEY_B]


def test_customer_tenant_never_touches_house_keys_even_when_its_key_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = FakeProvider({CUSTOMER_REF: 401})
    handler = _handler(monkeypatch, provider, Pool(KEY_A, KEY_B), Clock())

    result = _run(handler, CUSTOMER_REF)

    assert not result.success
    assert result.failure_class is Failure.PROVIDER_AUTH_FAILED
    assert provider.calls == [CUSTOMER_REF]
    assert result.secret_ref == CUSTOMER_REF
    assert result.key_failover_attempts == ()


def test_bad_key_stays_out_for_the_cooldown_and_returns_after_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = Clock()
    provider = FakeProvider({KEY_A: 401})
    handler = _handler(monkeypatch, provider, Pool(KEY_A, KEY_B), clock, cooldown=60.0)

    _run(handler, KEY_A)
    assert provider.calls == [KEY_A, KEY_B]

    clock.now += 59.0
    provider.calls.clear()
    held = _run(handler, KEY_A)
    assert provider.calls == [KEY_B]
    assert held.secret_ref == KEY_B
    assert held.key_failover_attempts[0].skipped_cooling

    clock.now += 2.0
    provider.answers.clear()  # the key was fixed while it was held out
    provider.calls.clear()
    back = _run(handler, KEY_A)
    assert provider.calls == [KEY_A]
    assert back.secret_ref == KEY_A
