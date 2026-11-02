# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The Claude review gateway spends only its own host's keys, fill-first (OMN-21014).

Every key here is a fixed fake string; no test reaches a provider.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from click.testing import CliRunner
from fastapi.testclient import TestClient
from pydantic import ValidationError

from omnimarket.cli import cli_secret
from omnimarket.enums.enum_anthropic_stop_reason import EnumAnthropicStopReason
from omnimarket.enums.enum_delegation_failure_class import EnumDelegationFailureClass
from omnimarket.events.llm_delegation_call import ModelLlmDelegationCallRequest
from omnimarket.inference.local_byok_credential_adapter import LocalByokCredentialStore
from omnimarket.nodes.node_claude_review_gateway_effect.handlers.handler_claude_review_gateway import (
    BLOCK_BOUNDARY_MARKER,
    CHAT_ROUTE,
    GATEWAY_PLAN,
    MODELS_ROUTE,
    SHARED_SECRET_ENV,
    SIGNATURE_HEADER,
    ClaudeReviewGateway,
    build_app,
    select_host_keys,
    serve_chat_call,
    translate_chat_request,
)
from omnimarket.nodes.node_claude_review_gateway_effect.models.model_claude_review_gateway import (
    ModelClaudeReviewGatewayRequest,
    ModelReviewGatewayOverlay,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers import (
    anthropic_messages,
)
from omnimarket.nodes.node_projection_tenant_credentials.handlers.handler_tenant_credentials_store import (
    apply_credential_registered,
)
from omnimarket.projection.sqlite_database import SqliteDatabaseAdapter

pytestmark = pytest.mark.unit

_TENANT = "house-tenant"
_HOST = "h201"
_MODEL = "claude-sonnet-5-5"
_URL = "https://provider.invalid/v1/chat/completions"
_SECRET = "test-shared-secret"
_NOW = datetime(2026, 10, 12, tzinfo=UTC)
#: Placeholder names: the real refused repository is named only in the private
#: overlay the gateway is started with, never in this public repository.
_REFUSED_REPO = "acme/private-notes"
_ALLOWED_REPO = "acme/private-infra"
_REFUSED = (_REFUSED_REPO,)


def _row(name: str, **extra: object) -> dict[str, object]:
    row: dict[str, object] = {
        "api_key_ref": f"cred_{_TENANT}_anthropic_{name}",
        "tenant_id": _TENANT,
        "provider": "anthropic",
        "name": name,
        "bound_host": _HOST,
        "revoked_at": None,
        "expires_at": None,
    }
    row.update(extra)
    return row


def _value(ref: str) -> str:
    return f"sk-ant-fake-{ref[-6:]}"


class FakeCatalog:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows

    def query(
        self, table: str, filters: dict[str, object] | None = None
    ) -> list[dict[str, object]]:
        assert table == "tenant_inference_credentials"
        return [
            row
            for row in self.rows
            if all(row.get(key) == value for key, value in (filters or {}).items())
        ]


class FakeValues:
    def __init__(self, refs: list[str]) -> None:
        self.values = {ref: _value(ref) for ref in refs}
        self.reads: list[str] = []

    async def get_secret(self, key: str) -> str | None:
        self.reads.append(key)
        return self.values.get(key)


class FakeOutcome:
    def __init__(
        self,
        *,
        success: bool = True,
        failure_class: EnumDelegationFailureClass | None = None,
        error_message: str | None = None,
        content: str = "ok",
        stop_reason: EnumAnthropicStopReason | None = EnumAnthropicStopReason.END_TURN,
        cache_creation_input_tokens: int = 0,
        cache_read_input_tokens: int = 0,
    ) -> None:
        self.success = success
        self.content = content if success else None
        self.failure_class = failure_class
        self.error_message = error_message
        self.tokens_in = 10
        self.tokens_out = 2
        self.stop_reason = stop_reason
        self.cache_creation_input_tokens = cache_creation_input_tokens
        self.cache_read_input_tokens = cache_read_input_tokens


class FakeCall:
    """Stands in for the delegation call effect; records each call request."""

    def __init__(self, outcomes: list[FakeOutcome]) -> None:
        self.outcomes = outcomes
        self.calls: list[ModelLlmDelegationCallRequest] = []

    def __call__(self, request: ModelLlmDelegationCallRequest) -> FakeOutcome:
        self.calls.append(request)
        return self.outcomes.pop(0)


class FakeEventBus:
    def __init__(self) -> None:
        self.events: list[tuple[str, bytes | None, bytes]] = []

    async def publish(self, topic: str, key: bytes | None, value: bytes) -> None:
        self.events.append((topic, key, value))


def _gateway(
    rows: list[dict[str, object]],
    outcomes: list[FakeOutcome] | None = None,
    *,
    resolvable: list[str] | None = None,
) -> tuple[ClaudeReviewGateway, FakeValues, FakeCall]:
    refs = (
        resolvable
        if resolvable is not None
        else [str(row["api_key_ref"]) for row in rows]
    )
    values = FakeValues(refs)
    call = FakeCall(list(outcomes or []))
    gateway = ClaudeReviewGateway(
        ModelClaudeReviewGatewayRequest(
            host=_HOST,
            tenant_id=_TENANT,
            model=_MODEL,
            cloud_refused_repositories=_REFUSED,
        ),
        catalog=FakeCatalog(rows),
        key_values=values,
        call=call,
        endpoint_url=_URL,
        overlay=ModelReviewGatewayOverlay(key_set_aside_ttl_seconds=17),
        event_bus=FakeEventBus(),
    )
    return gateway, values, call


_PAYLOAD: dict[str, Any] = {
    "model": "whatever-the-voter-read",
    "messages": [
        {"role": "system", "content": "You review code."},
        {"role": "user", "content": [{"type": "text", "text": "review this"}]},
    ],
    "max_tokens": 64,
    "temperature": 0.3,
    "top_p": 0.9,
    "metadata": {"repository": _ALLOWED_REPO},
}


def _for(repository: str | None) -> dict[str, Any]:
    """The review payload for one repository (None: the payload names none)."""
    payload = {key: value for key, value in _PAYLOAD.items() if key != "metadata"}
    if repository is not None:
        payload["metadata"] = {"repository": repository}
    return payload


def _ok(**fields: Any) -> FakeOutcome:
    return FakeOutcome(**fields)


def _fail(kind: EnumDelegationFailureClass, message: str = "") -> FakeOutcome:
    return FakeOutcome(success=False, failure_class=kind, error_message=message)


# --- AC1: which key -----------------------------------------------------------


def test_key_choice_is_this_hosts_live_anthropic_keys_in_name_order() -> None:
    rows = [
        _row("key-b"),
        _row("key-a"),
        _row("other-host", bound_host="h101"),
        _row("unbound", bound_host=None),
        _row("revoked", revoked_at="2026-10-01T00:00:00+00:00"),
        _row("expired", expires_at="2026-10-01T00:00:00+00:00"),
        _row("unreadable-expiry", expires_at="not a date"),
        _row("other-provider", provider="openrouter"),
        _row("other-tenant", tenant_id="someone-else"),
        _row("future", expires_at=(_NOW + timedelta(days=80)).isoformat()),
    ]
    keys = select_host_keys(rows, tenant_id=_TENANT, host=_HOST, now=_NOW)
    assert [key.name for key in keys] == ["future", "key-a", "key-b"]


def test_key_choice_fills_first_and_never_round_robins() -> None:
    gateway, _values, call = _gateway(
        [_row("key-b"), _row("key-a")], [_ok(), _ok(), _ok()]
    )
    for _ in range(3):
        status, reply = asyncio.run(gateway.complete(_PAYLOAD))
        assert status == 200
        assert reply["choices"][0]["message"]["content"] == "ok"
    assert {request.secret_ref for request in call.calls} == {
        f"cred_{_TENANT}_anthropic_key-a"
    }


def test_key_choice_translation_pins_the_model_and_drops_sampling() -> None:
    request = translate_chat_request(
        _PAYLOAD,
        model=_MODEL,
        endpoint_url=_URL,
        secret_ref="cred_ref",
        timeout_seconds=30.0,
    )
    assert request.model_id == _MODEL
    assert request.endpoint_ref == _URL
    assert request.secret_ref == "cred_ref"
    assert request.inference_protocol == "anthropic_messages"
    assert [b.text for b in request.system_blocks] == ["You review code."]
    assert [b.text for m in request.messages for b in m.blocks] == ["review this"]
    assert request.prompt == ""
    assert request.system_prompt == ""
    assert request.max_tokens == 64
    assert request.temperature == 1.0
    assert request.provider == "anthropic"
    assert request.tool_schema is None
    assert request.tool_choice is None


# --- OMN-20910: native Messages protocol behind the same voter surface -------


def _marked_payload(**extra: Any) -> dict[str, Any]:
    marker = BLOCK_BOUNDARY_MARKER
    return {
        **_PAYLOAD,
        "messages": [
            {
                "role": "system",
                "content": f"instructions and catalog\n{marker}\nstanding rules",
            },
            {
                "role": "user",
                "content": f"the diff\n{marker}\npersona focus: correctness",
            },
        ],
        **extra,
    }


def _translate(payload: dict[str, Any]) -> ModelLlmDelegationCallRequest:
    return translate_chat_request(
        payload,
        model=_MODEL,
        endpoint_url=_URL,
        secret_ref="cred_ref",
        timeout_seconds=30.0,
    )


def test_explicit_reasoning_off_disables_native_thinking() -> None:
    request = _translate(
        _marked_payload(chat_template_kwargs={"enable_thinking": False})
    )
    assert request.provider_request_options == {"thinking": {"type": "disabled"}}
    assert anthropic_messages.build_messages_payload(request)["thinking"] == {
        "type": "disabled"
    }


def test_opus_review_request_never_sends_disabled_thinking() -> None:
    request = translate_chat_request(
        _marked_payload(chat_template_kwargs={"enable_thinking": False}),
        model="claude-opus-5-5",
        endpoint_url=_URL,
        secret_ref="cred_ref",
        timeout_seconds=30,
        thinking_mode="adaptive",
    )
    assert request.model_id == "claude-opus-5-5"
    payload = anthropic_messages.build_messages_payload(request)
    assert payload["thinking"] == {"type": "adaptive"}
    assert payload["output_config"] == {"effort": "low"}


@pytest.mark.parametrize(
    "extra",
    [
        {},
        {"chat_template_kwargs": {"enable_thinking": True}},
        {"chat_template_kwargs": {"enable_thinking": "false"}},
        {"chat_template_kwargs": {"enable_thinking": 0}},
        {"chat_template_kwargs": "x"},
        {"chat_template_kwargs": {}},
    ],
)
def test_other_reasoning_values_send_no_native_thinking(
    extra: dict[str, object],
) -> None:
    request = _translate(_marked_payload(**extra))
    assert request.provider_request_options == {}
    assert "thinking" not in anthropic_messages.build_messages_payload(request)


def test_dry_run_shows_the_explicit_reasoning_off_request() -> None:
    gateway, _values, _call = _gateway([_row("key-a")])
    plan = asyncio.run(
        gateway.plan(_marked_payload(chat_template_kwargs={"enable_thinking": False}))
    )
    assert plan.translated_request["thinking"] == {"type": "disabled"}
    default_plan = asyncio.run(gateway.plan(_PAYLOAD))
    assert default_plan.translated_request["thinking"] is None


def test_blocks_split_at_the_boundary_with_the_persona_last_and_uncached() -> None:
    request = _translate(_marked_payload())
    assert [(b.text, b.cache_ttl) for b in request.system_blocks] == [
        ("instructions and catalog", "1h"),
        ("standing rules", "1h"),
    ]
    (message,) = request.messages
    assert [(b.text, b.cache_ttl) for b in message.blocks] == [
        ("the diff", "5m"),
        ("persona focus: correctness", None),
    ]


def test_surplus_parts_join_the_last_block_so_breakpoints_stay_within_four() -> None:
    marker = BLOCK_BOUNDARY_MARKER
    payload = _marked_payload()
    payload["messages"][0]["content"] = f"a\n{marker}\nb\n{marker}\nc"
    payload["messages"][1]["content"] = f"d\n{marker}\ne\n{marker}\nf"
    request = _translate(payload)
    assert [b.text for b in request.system_blocks] == ["a", "b\n\nc"]
    assert [b.text for b in request.messages[0].blocks] == ["d", "e\n\nf"]


def test_json_schema_response_format_becomes_a_forced_tool() -> None:
    schema = {"type": "object", "properties": {"f": {"type": "array"}}}
    request = _translate(
        _marked_payload(
            response_format={
                "type": "json_schema",
                "json_schema": {"name": "verdict", "schema": schema},
            }
        )
    )
    assert request.tool_schema is not None
    assert request.tool_schema["name"] == "verdict"
    assert request.tool_schema["input_schema"] == schema
    assert request.tool_choice == {"type": "tool", "name": "verdict"}


def test_json_object_response_format_calls_no_tool() -> None:
    request = _translate(_marked_payload(response_format={"type": "json_object"}))
    assert request.tool_schema is None
    assert request.tool_choice is None


def test_a_max_tokens_stop_is_answered_as_length_never_a_shortened_stop() -> None:
    gateway, _values, _call = _gateway(
        [_row("key-a")],
        [_ok(stop_reason=EnumAnthropicStopReason.MAX_TOKENS, content='{"f":[{"c":')],
    )
    status, reply = asyncio.run(gateway.complete(_PAYLOAD))
    assert status == 200
    assert reply["choices"][0]["finish_reason"] == "length"
    assert reply["choices"][0]["message"]["content"] == '{"f":[{"c":'


def test_a_normal_end_is_answered_as_stop_with_cache_usage_in_the_reply() -> None:
    gateway, _values, _call = _gateway(
        [_row("key-a")],
        [
            _ok(
                stop_reason=EnumAnthropicStopReason.TOOL_USE,
                cache_creation_input_tokens=5,
                cache_read_input_tokens=7,
            )
        ],
    )
    status, reply = asyncio.run(gateway.complete(_PAYLOAD))
    assert status == 200
    assert reply["choices"][0]["finish_reason"] == "stop"
    assert reply["usage"]["cache_read_input_tokens"] == 7
    assert reply["usage"]["cache_creation_input_tokens"] == 5
    assert reply["usage"]["prompt_tokens_details"] == {"cached_tokens": 7}


def test_a_review_with_no_user_message_is_a_400_before_any_key_is_read() -> None:
    gateway, values, call = _gateway([_row("key-a")], [_ok()])
    payload = {**_PAYLOAD, "messages": [{"role": "system", "content": "only system"}]}
    status, reply = asyncio.run(gateway.complete(payload))
    assert status == 400
    assert reply["error"]["type"] == "invalid_request"
    assert call.calls == []
    assert values.reads == []


def test_the_gateway_resolves_the_native_plan_and_not_the_default_row() -> None:
    from omnimarket.handlers.routing.byok_provider_backends import (
        resolve_byok_provider_backend,
    )

    native = resolve_byok_provider_backend("anthropic", GATEWAY_PLAN)
    default = resolve_byok_provider_backend("anthropic")
    assert native is not None
    assert default is not None
    assert native.endpoint_url.endswith("/v1/messages")
    assert native.inference_protocol == "anthropic_messages"
    assert default.endpoint_url.endswith("/v1/chat/completions")
    assert default.inference_protocol == "openai_chat"


# --- AC2: 503 and fail-over ---------------------------------------------------


def test_fails_over_to_503_with_no_upstream_call_when_no_key_is_bound_here() -> None:
    gateway, values, call = _gateway([_row("elsewhere", bound_host="h101")])
    status, reply = asyncio.run(gateway.complete(_PAYLOAD))
    assert status == 503
    assert reply["error"]["type"] == "gateway_no_key"
    assert call.calls == []
    assert values.reads == []


@pytest.mark.parametrize(
    "failure",
    [
        EnumDelegationFailureClass.RATE_LIMITED,
        EnumDelegationFailureClass.TIMEOUT,
        EnumDelegationFailureClass.MODEL_UNAVAILABLE,
        EnumDelegationFailureClass.CONTEXT_TOO_LARGE,
        EnumDelegationFailureClass.UNKNOWN,
    ],
)
def test_fails_over_with_503_on_a_busy_or_absent_provider_without_trying_other_keys(
    failure: EnumDelegationFailureClass,
) -> None:
    gateway, _values, call = _gateway([_row("key-a"), _row("key-b")], [_fail(failure)])
    status, _reply = asyncio.run(gateway.complete(_PAYLOAD))
    assert status == 503
    assert len(call.calls) == 1
    assert gateway.set_aside == frozenset()


@pytest.mark.parametrize(
    "refusal",
    [
        _fail(EnumDelegationFailureClass.PROVIDER_AUTH_FAILED, "invalid x-api-key"),
        _fail(EnumDelegationFailureClass.PROVIDER_BILLING, "PROVIDER_BILLING"),
        _fail(EnumDelegationFailureClass.PROVIDER_CREDENTIAL_MISSING),
        _fail(
            EnumDelegationFailureClass.UNKNOWN,
            "Your credit balance is too low to access the Anthropic API.",
        ),
        _fail(EnumDelegationFailureClass.RATE_LIMITED, "enforced_spend_limit_reached"),
    ],
)
def test_fails_over_to_the_next_key_on_a_refused_or_exhausted_key(
    refusal: FakeOutcome,
) -> None:
    gateway, _values, call = _gateway([_row("key-a"), _row("key-b")], [refusal, _ok()])
    status, _reply = asyncio.run(gateway.complete(_PAYLOAD))
    assert status == 200
    assert [request.secret_ref for request in call.calls] == [
        f"cred_{_TENANT}_anthropic_key-a",
        f"cred_{_TENANT}_anthropic_key-b",
    ]
    assert gateway.set_aside == frozenset({f"cred_{_TENANT}_anthropic_key-a"})


def test_fails_over_to_503_once_every_key_is_set_aside() -> None:
    refused = _fail(EnumDelegationFailureClass.PROVIDER_AUTH_FAILED)
    gateway, _values, call = _gateway([_row("key-a")], [refused])
    assert asyncio.run(gateway.complete(_PAYLOAD))[0] == 503
    status, reply = asyncio.run(gateway.complete(_PAYLOAD))
    assert status == 503
    assert reply["error"]["type"] == "gateway_no_key"
    assert len(call.calls) == 1


def test_fails_over_past_a_key_whose_value_does_not_resolve() -> None:
    gateway, _values, call = _gateway(
        [_row("key-a"), _row("key-b")],
        [_ok()],
        resolvable=[f"cred_{_TENANT}_anthropic_key-b"],
    )
    assert asyncio.run(gateway.complete(_PAYLOAD))[0] == 200
    assert [request.secret_ref for request in call.calls] == [
        f"cred_{_TENANT}_anthropic_key-b"
    ]


# --- privacy: one refused repository, every other one served -----------------


@pytest.mark.parametrize(
    "repository",
    [_REFUSED_REPO, "private-notes", "private_notes", "ACME/Private_Notes"],
)
def test_privacy_a_refused_repositorys_diff_never_reaches_the_cloud(
    repository: str,
) -> None:
    gateway, values, call = _gateway([_row("key-a")], [_ok()])
    status, reply = asyncio.run(gateway.complete(_for(repository)))
    assert status == 503
    assert reply["error"]["type"] == "gateway_repository_refused"
    assert values.reads == []
    assert call.calls == []


def test_privacy_a_private_repository_not_on_the_list_is_served() -> None:
    gateway, _values, call = _gateway([_row("key-a")], [_ok()])
    status, _reply = asyncio.run(gateway.complete(_for(_ALLOWED_REPO)))
    assert status == 200
    assert len(call.calls) == 1


def test_privacy_a_review_that_names_no_repository_is_refused() -> None:
    gateway, values, call = _gateway([_row("key-a")], [_ok()])
    for payload in (_for(None), {**_for(None), "metadata": {"repository": "  "}}):
        status, reply = asyncio.run(gateway.complete(payload))
        assert status == 503
        assert reply["error"]["type"] == "gateway_repository_unnamed"
    assert values.reads == []
    assert call.calls == []


def test_privacy_the_signed_route_refuses_a_refused_repository() -> None:
    gateway, values, call = _gateway([_row("key-a")], [_ok()])
    payload = _for(_REFUSED_REPO)
    status, reply = asyncio.run(
        serve_chat_call(
            gateway,
            body=_wire(payload),
            signature=_signed(payload)[SIGNATURE_HEADER],
            secret=_SECRET,
        )
    )
    assert status == 503
    assert reply["error"]["type"] == "gateway_repository_refused"
    assert values.reads == []
    assert call.calls == []


def test_privacy_the_dry_run_would_refuse_a_refused_repository() -> None:
    gateway, _values, call = _gateway([_row("key-a")])
    plan = asyncio.run(gateway.plan(_for(_REFUSED_REPO)))
    assert plan.would_answer == 503
    assert plan.chosen_key_ref is None
    assert "refused" in plan.reason
    assert call.calls == []


def test_privacy_a_gateway_with_no_refused_list_is_refused() -> None:
    with pytest.raises(ValidationError):
        ModelClaudeReviewGatewayRequest(
            host=_HOST,
            tenant_id=_TENANT,
            model=_MODEL,
            cloud_refused_repositories=(),
        )


# --- AC3: signature and no key value anywhere ---------------------------------


def _wire(payload: dict[str, Any]) -> bytes:
    """The body as the voter transport sends it: JSON bytes, signed separately."""
    return json.dumps(payload).encode("utf-8")


def _signed(payload: dict[str, Any], secret: str = _SECRET) -> dict[str, str]:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return {
        SIGNATURE_HEADER: hmac.new(
            secret.encode(), canonical, hashlib.sha256
        ).hexdigest()
    }


@pytest.mark.parametrize(
    "signature",
    [None, "", "0" * 64, "wrong-secret"],
    ids=["none", "empty", "zeros", "wrong"],
)
def test_signature_missing_or_wrong_is_refused_before_any_key_is_read(
    signature: str | None,
) -> None:
    gateway, values, call = _gateway([_row("key-a")], [_ok()])
    sent = (
        _signed(_PAYLOAD, "another-secret")[SIGNATURE_HEADER]
        if signature == "wrong-secret"
        else signature
    )
    status, reply = asyncio.run(
        serve_chat_call(gateway, body=_wire(_PAYLOAD), signature=sent, secret=_SECRET)
    )
    assert status == 401
    assert reply["error"]["type"] == "gateway_signature"
    assert values.reads == []
    assert call.calls == []


def test_signature_of_one_payload_does_not_cover_a_changed_payload() -> None:
    gateway, values, call = _gateway([_row("key-a")], [_ok()])
    tampered = {**_PAYLOAD, "model": "another-model"}
    status, reply = asyncio.run(
        serve_chat_call(
            gateway,
            body=_wire(tampered),
            signature=_signed(_PAYLOAD)[SIGNATURE_HEADER],
            secret=_SECRET,
        )
    )
    assert status == 401
    assert reply["error"]["type"] == "gateway_signature"
    assert values.reads == []
    assert call.calls == []


@pytest.mark.parametrize("body", [b"not json", b"[1, 2]"])
def test_signature_check_refuses_a_body_that_is_not_a_json_object(body: bytes) -> None:
    gateway, values, call = _gateway([_row("key-a")], [_ok()])
    status, _reply = asyncio.run(
        serve_chat_call(gateway, body=body, signature="x", secret=_SECRET)
    )
    assert status == 400
    assert values.reads == []
    assert call.calls == []


def test_signature_unset_on_the_gateway_refuses_every_call() -> None:
    gateway, values, _call = _gateway([_row("key-a")], [_ok()])
    status, reply = asyncio.run(
        serve_chat_call(
            gateway,
            body=_wire(_PAYLOAD),
            signature=_signed(_PAYLOAD)[SIGNATURE_HEADER],
            secret="",
        )
    )
    assert status == 503
    assert reply["error"]["type"] == "gateway_not_configured"
    assert values.reads == []


def test_signature_route_refuses_an_unsigned_post_through_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The HTTP route is wired to the same check (a body with no review in it)."""
    monkeypatch.setenv(SHARED_SECRET_ENV, _SECRET)
    gateway, values, call = _gateway([_row("key-a")], [_ok()])
    client = TestClient(build_app(gateway, model=_MODEL))
    response = client.post(CHAT_ROUTE, content=b"{}")
    assert response.status_code == 401
    assert values.reads == []
    assert call.calls == []


def test_signature_valid_is_served_and_no_key_value_reaches_the_reply_or_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    refused = _fail(EnumDelegationFailureClass.PROVIDER_AUTH_FAILED)
    gateway, values, call = _gateway([_row("key-a"), _row("key-b")], [refused, _ok()])
    client = TestClient(build_app(gateway, model=_MODEL))
    with caplog.at_level(logging.DEBUG):
        status, reply = asyncio.run(
            serve_chat_call(
                gateway,
                body=_wire(_PAYLOAD),
                signature=_signed(_PAYLOAD)[SIGNATURE_HEADER],
                secret=_SECRET,
            )
        )
        models = client.get(MODELS_ROUTE)
    assert status == 200
    assert models.json()["data"] == [{"id": _MODEL, "object": "model"}]
    served = json.dumps(reply)
    for value in values.values.values():
        assert value not in served
        assert value not in models.text
        assert value not in caplog.text
        # The gateway hands the call effect a reference, never a value.
        assert all(value not in request.model_dump_json() for request in call.calls)


def _upstream_call_for(body: bytes, signature: str) -> tuple[int, list[str]]:
    """Serve one body through the signed route; return the status and upstream calls.

    Each call carries fresh request ids; everything else is what was acted on.
    """
    gateway, _values, call = _gateway([_row("key-a")], [_ok()])
    status, _reply = asyncio.run(
        serve_chat_call(gateway, body=body, signature=signature, secret=_SECRET)
    )
    per_call_ids = {"request_id", "correlation_id", "causation_id"}
    return status, [
        request.model_dump_json(exclude=per_call_ids) for request in call.calls
    ]


def _with_leading_duplicate(payload: dict[str, Any], key: str, value: Any) -> bytes:
    """The wire body with ``key`` sent twice: ``value`` first, the payload's own last."""
    rest = json.dumps(payload)
    return (json.dumps({key: value})[:-1] + ", " + rest[1:]).encode("utf-8")


def _with_trailing_duplicate(payload: dict[str, Any], key: str, value: Any) -> bytes:
    """The wire body with ``key`` sent twice: the payload's own first, ``value`` last."""
    rest = json.dumps(payload)
    return (rest[:-1] + ", " + json.dumps({key: value})[1:]).encode("utf-8")


def test_signature_a_reordered_body_is_acted_on_exactly_as_signed() -> None:
    """Key order on the wire cannot change what the gateway sends upstream."""
    signature = _signed(_PAYLOAD)[SIGNATURE_HEADER]
    reordered = json.dumps(dict(reversed(list(_PAYLOAD.items())))).encode("utf-8")
    assert reordered != _wire(_PAYLOAD)
    status, calls = _upstream_call_for(reordered, signature)
    baseline_status, baseline_calls = _upstream_call_for(_wire(_PAYLOAD), signature)
    assert (status, baseline_status) == (200, 200)
    assert calls == baseline_calls
    assert len(calls) == 1


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("model", "another-model"),
        ("max_tokens", 100000),
        ("metadata", {"repository": _REFUSED_REPO}),
    ],
    ids=["model", "max_tokens", "repository"],
)
def test_signature_a_duplicate_key_body_cannot_change_what_is_acted_on(
    key: str, value: Any
) -> None:
    """A duplicated key is acted on only as the signed payload, or refused.

    The body is parsed once and the same parsed object is both verified and
    served. A duplicate that ends on the signed value is served exactly as the
    signed payload; one that ends on another value fails the signature, before
    any key is read.
    """
    signature = _signed(_PAYLOAD)[SIGNATURE_HEADER]
    _status, baseline_calls = _upstream_call_for(_wire(_PAYLOAD), signature)

    status, calls = _upstream_call_for(
        _with_leading_duplicate(_PAYLOAD, key, value), signature
    )
    assert status == 200
    assert calls == baseline_calls

    gateway, values, call = _gateway([_row("key-a")], [_ok()])
    status, reply = asyncio.run(
        serve_chat_call(
            gateway,
            body=_with_trailing_duplicate(_PAYLOAD, key, value),
            signature=signature,
            secret=_SECRET,
        )
    )
    assert status == 401
    assert reply["error"]["type"] == "gateway_signature"
    assert values.reads == []
    assert call.calls == []


# --- AC4: the dry run ---------------------------------------------------------


def test_dry_run_plans_the_key_and_body_without_an_upstream_call() -> None:
    gateway, _values, call = _gateway([_row("key-b"), _row("key-a")])
    plan = asyncio.run(gateway.plan(_PAYLOAD))
    assert plan.host == _HOST
    assert plan.chosen_key_ref == f"cred_{_TENANT}_anthropic_key-a"
    assert plan.would_answer == 200
    assert plan.translated_request["secret_ref"] == plan.chosen_key_ref
    assert plan.translated_request["temperature"] == 1.0
    assert call.calls == []


def test_dry_run_with_no_key_bound_here_would_answer_503() -> None:
    gateway, _values, call = _gateway([])
    plan = asyncio.run(gateway.plan(_PAYLOAD))
    assert plan.chosen_key_ref is None
    assert plan.would_answer == 503
    assert "bound to host h201" in plan.reason
    assert call.calls == []


@pytest.mark.parametrize("thinking_mode", ["disabled", "adaptive"])
def test_dry_run_command_reads_this_machines_catalog_and_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, thinking_mode: str
) -> None:
    store = LocalByokCredentialStore(db_path=tmp_path / "delegation.sqlite")
    ref = f"cred_{_TENANT}_anthropic_" + "a" * 32
    asyncio.run(store.set_secret(ref, "sk-ant-fake-never-printed"))
    apply_credential_registered(
        {
            "tenant_id": _TENANT,
            "provider": "anthropic",
            "name": "claude-review-h201",
            "api_key_ref": ref,
            "bound_host": _HOST,
            "expires_at": "2099-01-01T00:00:00+00:00",
        },
        SqliteDatabaseAdapter(store.db_path),
    )
    table = tmp_path / "hosts.yaml"
    table.write_text(
        "hosts:\n  - name: h201\n    target: lab-host-201\n", encoding="utf-8"
    )
    monkeypatch.setenv("ONEX_LAB_RUN_HOSTS", str(table))
    policy = tmp_path / "gateway-policy.yaml"
    policy.write_text("key_set_aside_ttl_seconds: 17\n", encoding="utf-8")
    monkeypatch.setenv("ONEX_REVIEW_GATEWAY_OVERLAY", str(policy))
    monkeypatch.setattr(cli_secret, "_tenant_key_store", lambda: store)

    def _no_upstream(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("a dry run made an upstream call")

    monkeypatch.setattr(
        "omnimarket.nodes.node_llm_delegation_call_effect.handlers."
        "handler_llm_delegation_call.HandlerLlmDelegationCall.__call__",
        _no_upstream,
    )
    overlay = tmp_path / "overlay.yaml"
    overlay.write_text(
        f"cloud_review_refused_repositories:\n  - {_REFUSED_REPO}\n", encoding="utf-8"
    )
    command = [
        "review-gateway",
        "--tenant",
        _TENANT,
        "--model",
        _MODEL,
        "--host",
        _HOST,
        "--privacy-overlay",
        str(overlay),
        "--dry-run",
        "--thinking-mode",
        thinking_mode,
    ]
    result = CliRunner().invoke(
        cli_secret.models_group, [*command, "--repository", _ALLOWED_REPO]
    )
    assert result.exit_code == 0, result.output
    plan = json.loads(result.output)
    assert plan["host"] == _HOST
    assert plan["chosen_key_ref"] == ref
    assert plan["would_answer"] == 200
    assert plan["upstream_url"].startswith("https://")
    assert plan["translated_request"]["thinking"] == (
        {"type": "adaptive"} if thinking_mode == "adaptive" else None
    )
    assert "sk-ant-fake-never-printed" not in result.output

    refused = CliRunner().invoke(
        cli_secret.models_group, [*command, "--repository", _REFUSED_REPO]
    )
    assert refused.exit_code == 0, refused.output
    assert json.loads(refused.output)["would_answer"] == 503


@pytest.mark.parametrize(
    "overlay_text",
    [None, "cloud_review_refused_repositories: []\n", "other_key: [x]\n"],
    ids=["absent", "empty", "no-list"],
)
def test_privacy_the_command_will_not_start_without_a_refused_list(
    tmp_path: Path, overlay_text: str | None
) -> None:
    command = ["review-gateway", "--tenant", _TENANT, "--model", _MODEL]
    command += ["--host", _HOST, "--dry-run", "--repository", _ALLOWED_REPO]
    if overlay_text is not None:
        overlay = tmp_path / "overlay.yaml"
        overlay.write_text(overlay_text, encoding="utf-8")
        command += ["--privacy-overlay", str(overlay)]
    result = CliRunner().invoke(cli_secret.models_group, command)
    assert result.exit_code != 0
    assert "would_answer" not in result.output


@pytest.mark.parametrize("thinking", [False, True])
def test_adaptive_gateway_translates_the_review_reasoning_budget(
    thinking: bool,
) -> None:
    request = translate_chat_request(
        _marked_payload(chat_template_kwargs={"enable_thinking": thinking}),
        model=_MODEL,
        endpoint_url=_URL,
        secret_ref="cred_ref",
        timeout_seconds=30,
        thinking_mode="adaptive",
    )
    payload = anthropic_messages.build_messages_payload(request)
    assert payload["thinking"] == {"type": "adaptive"}
    if thinking is False:
        assert payload["output_config"] == {"effort": "low"}
    else:
        assert "output_config" not in payload


def test_gateway_thinking_mode_is_a_validated_deployment_fact() -> None:
    with pytest.raises(ValidationError):
        ModelClaudeReviewGatewayRequest(
            host=_HOST,
            tenant_id=_TENANT,
            model=_MODEL,
            cloud_refused_repositories=_REFUSED,
            thinking_mode="invented",
        )


def test_adaptive_gateway_plan_and_call_use_the_declared_mode() -> None:
    from omnimarket.nodes.node_claude_review_gateway_effect.handlers.handler_claude_review_gateway import (
        HandlerClaudeReviewGateway,
    )

    _unused_gateway, values, call = _gateway([_row("key-a")], [FakeOutcome()])
    adaptive = HandlerClaudeReviewGateway(
        catalog=FakeCatalog([_row("key-a")]),
        key_values=values,
        call=call,
        endpoint_url=_URL,
        overlay=ModelReviewGatewayOverlay(key_set_aside_ttl_seconds=17),
        event_bus=FakeEventBus(),
    ).gateway(
        ModelClaudeReviewGatewayRequest(
            host=_HOST,
            tenant_id=_TENANT,
            model="claude-opus-5-5",
            cloud_refused_repositories=_REFUSED,
            thinking_mode="adaptive",
        )
    )
    payload = _marked_payload(chat_template_kwargs={"enable_thinking": False})
    plan = asyncio.run(adaptive.plan(payload))
    assert plan.translated_request["thinking"] == {"type": "adaptive"}
    assert plan.translated_request["output_config"] == {"effort": "low"}
    status, _reply = asyncio.run(adaptive.complete(payload))
    assert status == 200
    assert call.calls[0].provider_request_options == {
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": "low"},
    }
    native = anthropic_messages.build_messages_payload(call.calls[0])
    assert native["model"] == "claude-opus-5-5"
    assert native["thinking"] == {"type": "adaptive"}
    assert native["output_config"] == {"effort": "low"}


def test_gateway_target_changes_only_with_its_declared_model() -> None:
    models = [_MODEL, "claude-opus-5-5"]
    requests = [
        translate_chat_request(
            _marked_payload(chat_template_kwargs={"enable_thinking": False}),
            model=model,
            endpoint_url=_URL,
            secret_ref="cred_ref",
            timeout_seconds=30,
            thinking_mode="adaptive",
        )
        for model in models
    ]
    assert [request.model_id for request in requests] == models
    assert all(request.endpoint_ref == _URL for request in requests)
    assert all(
        request.provider_request_options
        == {
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": "low"},
        }
        for request in requests
    )


async def test_key_set_aside_ttl_and_events_inmemory(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

    from omnimarket.nodes.node_claude_review_gateway_effect.models.model_claude_review_gateway import (
        ModelReviewGatewayOverlay,
    )

    bus = EventBusInmemory()
    await bus.start()
    clock = [_NOW]
    values = FakeValues([str(_row("key-a")["api_key_ref"])])
    secret = next(iter(values.values.values()))
    call = FakeCall(
        [
            _fail(
                EnumDelegationFailureClass.UNKNOWN,
                f"credit balance is too low {secret}",
            ),
            _fail(EnumDelegationFailureClass.PROVIDER_AUTH_FAILED, secret),
            _ok(),
        ]
    )
    gateway = ClaudeReviewGateway(
        ModelClaudeReviewGatewayRequest(
            host=_HOST,
            tenant_id=_TENANT,
            model=_MODEL,
            cloud_refused_repositories=_REFUSED,
        ),
        catalog=FakeCatalog([_row("key-a")]),
        key_values=values,
        call=call,
        endpoint_url=_URL,
        overlay=ModelReviewGatewayOverlay(key_set_aside_ttl_seconds=17),
        event_bus=bus,
        clock=lambda: clock[0],
    )
    events: list[dict[str, Any]] = []
    emitted_topics: list[str] = []

    async def observe(message: Any) -> None:
        events.append(json.loads(message.value))
        emitted_topics.append(message.topic)

    for topic in gateway.event_topics:
        await bus.subscribe(topic, on_message=observe, group_id="gateway-ttl-fixture")
    try:
        with caplog.at_level(logging.DEBUG):
            assert (await gateway.complete(_PAYLOAD))[0] == 503
            (record,) = gateway.set_aside_records
            assert record.cause == EnumDelegationFailureClass.PROVIDER_BILLING
            assert record.set_aside_at == _NOW
            assert secret not in record.model_dump_json()
            clock[0] += timedelta(seconds=16)
            assert (await gateway.complete(_PAYLOAD))[0] == 503
            assert len(call.calls) == 1
            assert len(events) == 1
            clock[0] += timedelta(seconds=1)
            assert (await gateway.complete(_PAYLOAD))[0] == 503
            assert len(call.calls) == 2
            assert [event["transition"] for event in events] == [
                "set_aside",
                "retry",
                "set_aside",
            ]
            assert events[1]["set_aside_at"] == events[0]["set_aside_at"]
            assert events[1]["cause"] == events[0]["cause"]
            assert (await gateway.complete(_PAYLOAD))[0] == 503
            assert len(call.calls) == 2
            clock[0] += timedelta(seconds=17)
            assert (await gateway.complete(_PAYLOAD))[0] == 200
            assert gateway.set_aside_records == ()
            assert [event["transition"] for event in events] == [
                "set_aside",
                "retry",
                "set_aside",
                "retry",
            ]
            assert emitted_topics == [
                "onex.evt.omnimarket.review-gateway-key-set-aside.v1",
                "onex.evt.omnimarket.review-gateway-key-retry.v1",
                "onex.evt.omnimarket.review-gateway-key-set-aside.v1",
                "onex.evt.omnimarket.review-gateway-key-retry.v1",
            ]
            assert {event["key_ref"] for event in events} == {call.calls[0].secret_ref}
            assert all(event["host"] == _HOST for event in events)
            assert secret not in json.dumps(events)
            assert secret not in caplog.text
    finally:
        await bus.close()


async def test_only_one_concurrent_request_claims_an_expired_key() -> None:
    entered_retry = asyncio.Event()
    release_retry = asyncio.Event()

    class SlowRetryBus(FakeEventBus):
        async def publish(self, topic: str, key: bytes | None, value: bytes) -> None:
            if json.loads(value)["transition"] == "retry":
                entered_retry.set()
                await release_retry.wait()
            await super().publish(topic, key, value)

    clock = [_NOW]
    bus = SlowRetryBus()
    values = FakeValues([str(_row("key-a")["api_key_ref"])])
    call = FakeCall([_fail(EnumDelegationFailureClass.PROVIDER_BILLING)] * 2)
    gateway = ClaudeReviewGateway(
        ModelClaudeReviewGatewayRequest(
            host=_HOST,
            tenant_id=_TENANT,
            model=_MODEL,
            cloud_refused_repositories=_REFUSED,
        ),
        catalog=FakeCatalog([_row("key-a")]),
        key_values=values,
        call=call,
        endpoint_url=_URL,
        overlay=ModelReviewGatewayOverlay(key_set_aside_ttl_seconds=17),
        event_bus=bus,
        clock=lambda: clock[0],
    )
    assert (await gateway.complete(_PAYLOAD))[0] == 503
    clock[0] += timedelta(seconds=17)
    first = asyncio.create_task(gateway.complete(_PAYLOAD))
    second: asyncio.Task[tuple[int, dict[str, Any]]] | None = None
    try:
        await asyncio.wait_for(entered_retry.wait(), timeout=5)
        second = asyncio.create_task(gateway.complete(_PAYLOAD))
        await asyncio.sleep(0)
        assert not second.done()
        assert len(call.calls) == 1
    finally:
        release_retry.set()
        await first
        if second is not None:
            assert (await second)[0] == 503
    assert len(call.calls) == 2
    assert (await gateway.complete(_PAYLOAD))[0] == 503
    assert len(call.calls) == 2
    assert [json.loads(value)["transition"] for _, _, value in bus.events] == [
        "set_aside",
        "retry",
        "set_aside",
    ]


@pytest.mark.parametrize("failed_transition", ["set_aside", "retry"])
async def test_failed_bus_publication_is_replayed_before_more_key_calls(
    failed_transition: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    class FlakyBus(FakeEventBus):
        def __init__(self) -> None:
            super().__init__()
            self.failed = False
            self.attempts: list[dict[str, Any]] = []

        async def publish(self, topic: str, key: bytes | None, value: bytes) -> None:
            event = json.loads(value)
            self.attempts.append(event)
            if event["transition"] == failed_transition and not self.failed:
                self.failed = True
                raise OSError(
                    "fixture bus unavailable "
                    + _value(str(_row("key-a")["api_key_ref"]))
                )
            await super().publish(topic, key, value)

    clock = [_NOW]
    bus = FlakyBus()
    call = FakeCall([_fail(EnumDelegationFailureClass.PROVIDER_BILLING), _ok()])
    gateway = ClaudeReviewGateway(
        ModelClaudeReviewGatewayRequest(
            host=_HOST,
            tenant_id=_TENANT,
            model=_MODEL,
            cloud_refused_repositories=_REFUSED,
        ),
        catalog=FakeCatalog([_row("key-a")]),
        key_values=FakeValues([str(_row("key-a")["api_key_ref"])]),
        call=call,
        endpoint_url=_URL,
        overlay=ModelReviewGatewayOverlay(key_set_aside_ttl_seconds=17),
        event_bus=bus,
        clock=lambda: clock[0],
    )
    if failed_transition == "retry":
        assert (await gateway.complete(_PAYLOAD))[0] == 503
        clock[0] += timedelta(seconds=17)
    status, reply = await gateway.complete(_PAYLOAD)
    assert status == 503
    assert reply["error"]["type"] == "gateway_dependency_unavailable"
    secret = _value(str(_row("key-a")["api_key_ref"]))
    assert secret not in json.dumps(reply)
    assert secret not in caplog.text
    assert len(call.calls) == 1
    if failed_transition == "set_aside":
        assert (await gateway.complete(_PAYLOAD))[0] == 503
        assert len(call.calls) == 1
        clock[0] += timedelta(seconds=17)
    assert (await gateway.complete(_PAYLOAD))[0] == 200
    assert len(call.calls) == 2
    events = [json.loads(value) for _, _, value in bus.events]
    assert [event["transition"] for event in events] == ["set_aside", "retry"]
    replayed = [e for e in bus.attempts if e["transition"] == failed_transition]
    assert len(replayed) == 2
    assert replayed[0]["event_id"] == replayed[1]["event_id"]


async def test_a_raising_retry_starts_a_new_interval(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class RaisingCall(FakeCall):
        def __call__(self, request: ModelLlmDelegationCallRequest) -> FakeOutcome:
            if len(self.calls) == 1:
                self.calls.append(request)
                raise OSError(
                    "fixture call interrupted "
                    + _value(str(_row("key-a")["api_key_ref"]))
                )
            return super().__call__(request)

    clock = [_NOW]
    bus = FakeEventBus()
    call = RaisingCall([_fail(EnumDelegationFailureClass.PROVIDER_BILLING), _ok()])
    gateway = ClaudeReviewGateway(
        ModelClaudeReviewGatewayRequest(
            host=_HOST,
            tenant_id=_TENANT,
            model=_MODEL,
            cloud_refused_repositories=_REFUSED,
        ),
        catalog=FakeCatalog([_row("key-a")]),
        key_values=FakeValues([str(_row("key-a")["api_key_ref"])]),
        call=call,
        endpoint_url=_URL,
        overlay=ModelReviewGatewayOverlay(key_set_aside_ttl_seconds=17),
        event_bus=bus,
        clock=lambda: clock[0],
    )
    assert (await gateway.complete(_PAYLOAD))[0] == 503
    clock[0] += timedelta(seconds=17)
    status, reply = await gateway.complete(_PAYLOAD)
    assert status == 503
    assert reply["error"]["type"] == "gateway_dependency_unavailable"
    secret = _value(str(_row("key-a")["api_key_ref"]))
    assert secret not in json.dumps(reply)
    assert secret not in caplog.text
    assert (await gateway.complete(_PAYLOAD))[0] == 503
    assert len(call.calls) == 2
    assert gateway.set_aside_records[0].cause == EnumDelegationFailureClass.UNKNOWN
    clock[0] += timedelta(seconds=17)
    assert (await gateway.complete(_PAYLOAD))[0] == 200
    assert len(call.calls) == 3


async def test_missing_key_value_has_a_timed_refusal_and_can_recover() -> None:
    clock = [_NOW]
    bus = FakeEventBus()
    values = FakeValues([])
    call = FakeCall([_ok()])
    key_ref = str(_row("key-a")["api_key_ref"])
    gateway = ClaudeReviewGateway(
        ModelClaudeReviewGatewayRequest(
            host=_HOST,
            tenant_id=_TENANT,
            model=_MODEL,
            cloud_refused_repositories=_REFUSED,
        ),
        catalog=FakeCatalog([_row("key-a")]),
        key_values=values,
        call=call,
        endpoint_url=_URL,
        overlay=ModelReviewGatewayOverlay(key_set_aside_ttl_seconds=23),
        event_bus=bus,
        clock=lambda: clock[0],
    )
    assert (await gateway.complete(_PAYLOAD))[0] == 503
    assert gateway.set_aside_records[0].cause == (
        EnumDelegationFailureClass.PROVIDER_CREDENTIAL_MISSING
    )
    values.values[key_ref] = _value(key_ref)
    assert (await gateway.complete(_PAYLOAD))[0] == 503
    assert call.calls == []
    clock[0] += timedelta(seconds=23)
    assert (await gateway.complete(_PAYLOAD))[0] == 200
    assert len(call.calls) == 1
    assert [json.loads(value)["transition"] for _, _, value in bus.events] == [
        "set_aside",
        "retry",
    ]


@pytest.mark.parametrize("ttl", [0, -1, float("inf"), float("nan"), True, "17"])
def test_set_aside_interval_must_be_a_positive_finite_overlay_number(ttl: Any) -> None:
    with pytest.raises(ValidationError):
        ModelReviewGatewayOverlay(key_set_aside_ttl_seconds=ttl)


def test_handler_loads_required_ttl_from_overlay_without_a_default(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from omnimarket.handlers.node_overlay_reader import NodeOverlayError
    from omnimarket.nodes.node_claude_review_gateway_effect.handlers.handler_claude_review_gateway import (
        HandlerClaudeReviewGateway,
    )

    monkeypatch.delenv("ONEX_REVIEW_GATEWAY_OVERLAY", raising=False)
    monkeypatch.delenv("ONEX_SKILL_OVERLAY_ROOTS", raising=False)
    handler = HandlerClaudeReviewGateway(
        catalog=FakeCatalog([]),
        key_values=FakeValues([]),
        call=FakeCall([]),
        endpoint_url=_URL,
        event_bus=FakeEventBus(),
    )
    request = ModelClaudeReviewGatewayRequest(
        host=_HOST,
        tenant_id=_TENANT,
        model=_MODEL,
        cloud_refused_repositories=_REFUSED,
    )
    with pytest.raises(NodeOverlayError, match="key_set_aside_ttl_seconds"):
        handler.gateway(request)
    overlay = tmp_path / "policy.yaml"
    overlay.write_text("key_set_aside_ttl_seconds: 23\n", encoding="utf-8")
    monkeypatch.setenv("ONEX_REVIEW_GATEWAY_OVERLAY", str(overlay))
    assert handler.gateway(request).set_aside_records == ()


async def test_live_handler_owns_the_bus_lifecycle_and_signed_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import uvicorn
    from omnibase_infra.event_bus.event_bus_kafka import EventBusKafka

    from omnimarket.nodes.node_claude_review_gateway_effect.handlers import (
        handler_claude_review_gateway as handler_module,
    )
    from omnimarket.nodes.node_claude_review_gateway_effect.handlers.handler_claude_review_gateway import (
        HandlerClaudeReviewGateway,
    )

    lifecycle: list[str] = []

    class LifecycleBus(FakeEventBus):
        async def start(self) -> None:
            lifecycle.append("start")

        async def close(self) -> None:
            lifecycle.append("close")

    bus = LifecycleBus()
    monkeypatch.setenv("KAFKA_BOOTSTRAP_SERVERS", "broker.example.invalid:9092")
    monkeypatch.setenv(SHARED_SECRET_ENV, _SECRET)
    monkeypatch.setattr(EventBusKafka, "default", lambda: bus)

    served_gateways: list[ClaudeReviewGateway] = []
    real_build_app = handler_module.build_app

    def capturing_build_app(gateway: ClaudeReviewGateway, *, model: str) -> Any:
        served_gateways.append(gateway)
        return real_build_app(gateway, model=model)

    monkeypatch.setattr(handler_module, "build_app", capturing_build_app)

    class Server:
        def __init__(self, config: Any) -> None:
            self.config = config

        async def serve(self) -> None:
            assert lifecycle == ["start"]
            status, _reply = await serve_chat_call(
                served_gateways[0],
                body=_wire(_PAYLOAD),
                signature=_signed(_PAYLOAD)[SIGNATURE_HEADER],
                secret=_SECRET,
            )
            assert status == 503
            lifecycle.append("served")

    monkeypatch.setattr(uvicorn, "Server", Server)
    handler = HandlerClaudeReviewGateway(
        catalog=FakeCatalog([_row("key-a")]),
        key_values=FakeValues([str(_row("key-a")["api_key_ref"])]),
        call=FakeCall([_fail(EnumDelegationFailureClass.PROVIDER_BILLING)]),
        endpoint_url=_URL,
        overlay=ModelReviewGatewayOverlay(key_set_aside_ttl_seconds=23),
    )
    await handler.handle(
        ModelClaudeReviewGatewayRequest(
            host=_HOST,
            tenant_id=_TENANT,
            model=_MODEL,
            cloud_refused_repositories=_REFUSED,
        )
    )
    assert lifecycle == ["start", "served", "close"]
    assert len(bus.events) == 1
    assert json.loads(bus.events[0][2])["cause"] == "provider_billing"


@pytest_asyncio.fixture
async def review_gateway_redpanda_bus() -> AsyncIterator[Any]:
    """Use the declared test lane's transport, including its SASL policy."""
    from omnibase_infra.event_bus.event_bus_kafka import EventBusKafka
    from omnibase_infra.event_bus.models.config import ModelKafkaEventBusConfig

    from tests.bus_lane_guard import (
        LANE_OVERLAY_PATH,
        assert_broker_in_declared_lane,
        declared_test_lane,
        load_lane_overlay,
        resolve_test_bus_broker,
    )

    bootstrap = assert_broker_in_declared_lane(resolve_test_bus_broker())
    declaration = load_lane_overlay(LANE_OVERLAY_PATH)["lanes"][declared_test_lane()]
    protocol = declaration["security_protocol"]
    mechanism = declaration.get("sasl_mechanism")
    if protocol.startswith("SASL"):
        assert os.environ.get("KAFKA_SASL_USERNAME"), (
            "test lane needs KAFKA_SASL_USERNAME"
        )
        assert os.environ.get("KAFKA_SASL_PASSWORD"), (
            "test lane needs KAFKA_SASL_PASSWORD"
        )
    bus = EventBusKafka(
        config=ModelKafkaEventBusConfig(
            bootstrap_servers=bootstrap,
            environment="integration-test",
            security_protocol=protocol,
            sasl_mechanism=mechanism,
            sasl_plain_username=os.environ.get("KAFKA_SASL_USERNAME"),
            sasl_plain_password=os.environ.get("KAFKA_SASL_PASSWORD"),
            timeout_seconds=10,
            max_retry_attempts=1,
            auto_offset_reset="earliest",
            enable_idempotence=False,
        )
    )
    await bus.start()
    try:
        yield bus
    finally:
        await bus.close()


@pytest.mark.kafka
@pytest.mark.integration
async def test_key_set_aside_ttl_and_events_redpanda(
    review_gateway_redpanda_bus: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Same signed gateway path and fake-clock expiry, with broker readback."""
    from uuid import uuid4

    from omnimarket.nodes.node_claude_review_gateway_effect.handlers import (
        handler_claude_review_gateway as module,
    )

    contract_bus = module.load_packaged_contract_section(module._NODE_NAME, "event_bus")
    assert isinstance(contract_bus, dict)
    topics = [f"{topic}.it-{uuid4().hex}" for topic in contract_bus["publish_topics"]]
    # An isolated contract topic binding keeps fixture events off the live state topics.
    monkeypatch.setattr(
        module,
        "load_packaged_contract_section",
        lambda *_: {
            "publish_topics": topics,
            "subscribe_topics": [],
        },
    )
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def observe(message: Any) -> None:
        await queue.put(json.loads(message.value))

    unsubscribers = [
        await review_gateway_redpanda_bus.subscribe(
            topic,
            on_message=observe,
            group_id=f"gateway-ttl-{uuid4().hex}",
        )
        for topic in topics
    ]
    clock = [_NOW]
    values = FakeValues([str(_row("key-a")["api_key_ref"])])
    secret = next(iter(values.values.values()))
    call = FakeCall(
        [
            _fail(
                EnumDelegationFailureClass.UNKNOWN,
                f"credit balance is too low {secret}",
            ),
            _ok(),
        ]
    )
    gateway = ClaudeReviewGateway(
        ModelClaudeReviewGatewayRequest(
            host=_HOST,
            tenant_id=_TENANT,
            model=_MODEL,
            cloud_refused_repositories=_REFUSED,
        ),
        catalog=FakeCatalog([_row("key-a")]),
        key_values=values,
        call=call,
        endpoint_url=_URL,
        overlay=ModelReviewGatewayOverlay(key_set_aside_ttl_seconds=23),
        event_bus=review_gateway_redpanda_bus,
        clock=lambda: clock[0],
    )

    async def serve() -> int:
        return (
            await serve_chat_call(
                gateway,
                body=_wire(_PAYLOAD),
                signature=_signed(_PAYLOAD)[SIGNATURE_HEADER],
                secret=_SECRET,
            )
        )[0]

    try:
        with caplog.at_level(logging.INFO):
            assert await serve() == 503
            aside = await asyncio.wait_for(queue.get(), timeout=30)
            assert aside["transition"] == "set_aside"
            assert aside["cause"] == "provider_billing"
            assert aside["set_aside_at"] == _NOW.isoformat().replace("+00:00", "Z")
            clock[0] += timedelta(seconds=22)
            assert await serve() == 503
            assert len(call.calls) == 1
            assert queue.empty()
            clock[0] += timedelta(seconds=1)
            assert await serve() == 200
            retry = await asyncio.wait_for(queue.get(), timeout=30)
            assert retry["transition"] == "retry"
            assert retry["key_ref"] == aside["key_ref"]
            assert retry["cause"] == aside["cause"]
            assert retry["set_aside_at"] == aside["set_aside_at"]
            assert (
                datetime.fromisoformat(retry["occurred_at"])
                - datetime.fromisoformat(aside["set_aside_at"])
            ).total_seconds() == 23
            assert len(call.calls) == 2
            assert gateway.set_aside_records == ()
            assert secret not in json.dumps([aside, retry])
            assert secret not in caplog.text
    finally:
        for unsubscribe in unsubscribers:
            await unsubscribe()
