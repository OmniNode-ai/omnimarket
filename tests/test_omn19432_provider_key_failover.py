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


class _KeyedStore:
    """A read-only secret store holding synthetic values, one per key NAME."""

    def __init__(self, values: dict[str, str]) -> None:
        self._values = values

    async def get_secret(self, key: str) -> str | None:
        return self._values.get(key)

    async def set_secret(self, key: str, value: str) -> bool:
        raise RuntimeError("read-only")

    async def delete_secret(self, key: str) -> bool:
        raise RuntimeError("read-only")

    async def list_keys(self, prefix: str | None = None) -> list[str]:
        return []

    async def health_check(self) -> bool:
        return True

    async def close(self, timeout_seconds: float | None = None) -> None:
        return None


def test_real_http_fake_provider_401_then_200_names_the_second_key() -> None:
    """The real call effect over HTTP: a fake provider refuses key one and answers key two."""
    import http.server
    import json
    import threading

    from omnimarket.inference.secret_store_resolver import injected_secret_store

    synthetic_a = "synthetic-first-key-for-omn19432"
    synthetic_b = "synthetic-second-key-for-omn19432"
    seen: list[str] = []

    class Provider(http.server.BaseHTTPRequestHandler):
        def _send(self, status: int, body: dict[str, Any]) -> None:
            payload = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self) -> None:
            self._send(200, {"data": [{"id": "claude-sonnet-5-5"}]})

        def do_POST(self) -> None:
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            token = (self.headers.get("Authorization") or "").removeprefix("Bearer ")
            seen.append("first" if token == synthetic_a else "second")
            if token == synthetic_a:
                self._send(401, {"error": {"message": "invalid x-api-key"}})
                return
            self._send(
                200,
                {
                    "model": "claude-sonnet-5-5",
                    "choices": [
                        {"message": {"content": "ok"}, "finish_reason": "stop"}
                    ],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                },
            )

        def log_message(self, format: str, *args: object) -> None:
            return None

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/v1/chat/completions"
        handler = HandlerLlmDelegationCall()
        handler.key_failover = failover_module.ProviderKeyFailover(
            pool=Pool(KEY_A, KEY_B), clock=Clock()
        )
        request = _request(KEY_A).model_copy(update={"endpoint_ref": url})
        store = _KeyedStore({KEY_A: synthetic_a, KEY_B: synthetic_b})
        with injected_secret_store(store):  # type: ignore[arg-type]
            result = handler.call_endpoint(request, url)
    finally:
        server.shutdown()
        server.server_close()

    assert seen == ["first", "second"]
    assert result.success, result.error_message
    assert result.secret_ref == KEY_B
    assert [a.key_name for a in result.key_failover_attempts] == [KEY_A, KEY_B]
    assert synthetic_a not in repr(result)
    assert synthetic_b not in repr(result)
