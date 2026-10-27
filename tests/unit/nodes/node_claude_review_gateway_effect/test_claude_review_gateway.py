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
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
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
    from omnimarket.routing.byok_provider_backends import (
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
