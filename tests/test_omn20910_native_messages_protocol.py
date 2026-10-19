# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20910: the delegation call effect speaks the native Anthropic Messages protocol.

The provider is replaced by ``RecordedMessagesApi``, which replays the documented
response shapes of ``/v1/messages`` and simulates the prompt cache from the wire
payload it receives: a cached prefix is tools, then system, then messages, up to
a block that carries ``cache_control``. A layout that does not put the diff
before the persona text cannot pass the cache-read test, because the fake reads
the layout off the wire and nothing else. No key, no network.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import pytest
from pydantic import SecretStr, ValidationError

from omnimarket.enums.enum_anthropic_stop_reason import EnumAnthropicStopReason
from omnimarket.enums.enum_provider_finish_reason import EnumProviderFinishReason
from omnimarket.enums.enum_secret_source import EnumSecretSource
from omnimarket.events.llm_delegation_call import ModelLlmDelegationCallRequest
from omnimarket.models.delegation.prompt_text_block import (
    ModelPromptMessage,
    ModelPromptTextBlock,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers import (
    handler_llm_delegation_call as effect,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers import transport
from omnimarket.nodes.node_llm_delegation_call_effect.models.model_llm_delegation_call_observation import (
    ModelLlmDelegationCallObservation,
)
from omnimarket.nodes.node_review_response_parser_compute.handlers.handler_compact_strict_parse import (
    parse_compact_strict,
)
from omnimarket.review.response_parser import EnumParseFailureKind
from omnimarket.routing.byok_provider_backends import resolve_byok_provider_backend

pytestmark = pytest.mark.unit

_KEY = "synthetic-omn20910-key"
_MODEL = "claude-sonnet-5-5"
_REF = "house.anthropic.api_key"
_NATIVE_URL = "https://api.anthropic.com/v1/messages"
_CHAT_URL = "https://api.anthropic.com/v1/chat/completions"

_INSTRUCTIONS = "You review code and report defects as codes. " * 40
_CATALOG = "Defect catalog entries with codes and definitions. " * 60
_RULES = "Standing rules of this repository. " * 30
_DIFF = "diff --git a/src/mod.py b/src/mod.py changed lines " * 120


def _words(text: str) -> int:
    return len(text.split())


class RecordedMessagesApi:
    """Replays ``/v1/messages`` replies and simulates the prefix cache off the wire."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any], dict[str, str]]] = []
        self._written: set[str] = set()
        self.queued: dict[str, Any] | None = None

    @staticmethod
    def _blocks(payload: dict[str, Any]) -> list[dict[str, Any]]:
        """Every block of the request in prefix order: tools, system, messages."""
        blocks: list[dict[str, Any]] = [
            {"text": json.dumps(tool, sort_keys=True)}
            for tool in payload.get("tools", [])
        ]
        system = payload.get("system", [])
        blocks.extend(system if isinstance(system, list) else [{"text": system}])
        for message in payload["messages"]:
            content = message["content"]
            blocks.extend(content if isinstance(content, list) else [{"text": content}])
        return blocks

    def post(
        self,
        *,
        endpoint_url: str,
        payload: dict[str, Any],
        extra_headers: dict[str, str],
        **_: Any,
    ) -> transport.ModelTransportResponse:
        self.calls.append((endpoint_url, payload, extra_headers))
        if self.queued is not None:
            return transport.ModelTransportResponse(
                status_code=200, json_body=self.queued, latency_ms=7
            )
        read = write_5m = write_1h = covered = tokens = 0
        seen: list[str] = []
        for block in self._blocks(payload):
            seen.append(block["text"])
            tokens += _words(block["text"])
            control = block.get("cache_control")
            if control is None:
                continue
            key = "\x00".join(seen)
            if key in self._written:
                read = tokens
            elif control["ttl"] == "1h":
                write_1h += tokens - covered
            else:
                write_5m += tokens - covered
            self._written.add(key)
            covered = tokens
        uncached = tokens - covered
        return transport.ModelTransportResponse(
            status_code=200,
            json_body={
                "id": "msg_01",
                "type": "message",
                "role": "assistant",
                "model": _MODEL,
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_01",
                        "name": "submit_verdict",
                        "input": {"done": True, "more": False, "f": []},
                    }
                ],
                "stop_reason": "tool_use",
                "usage": {
                    "input_tokens": uncached,
                    "output_tokens": 31,
                    "cache_creation_input_tokens": write_5m + write_1h,
                    "cache_read_input_tokens": read,
                    "cache_creation": {
                        "ephemeral_5m_input_tokens": write_5m,
                        "ephemeral_1h_input_tokens": write_1h,
                    },
                },
            },
            latency_ms=7,
        )


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> RecordedMessagesApi:
    fake = RecordedMessagesApi()
    monkeypatch.setattr(transport, "post_chat_completion", fake.post)
    monkeypatch.setattr(effect, "_is_endpoint_healthy", lambda _: True)
    monkeypatch.setattr(
        effect,
        "resolve_api_key_with_source_loop_safe",
        lambda *_a, **_k: (SecretStr(_KEY), EnumSecretSource.LOCAL_STORE),
    )
    return fake


_TOOL = {
    "name": "submit_verdict",
    "description": "Submit the review verdict.",
    "input_schema": {"type": "object", "properties": {"f": {"type": "array"}}},
}


def _blocks(persona: str) -> dict[str, Any]:
    """Prefix order of the plan: tool, system A, system B, diff, then the persona."""
    return {
        "system_blocks": (
            ModelPromptTextBlock(text=_INSTRUCTIONS + _CATALOG, cache_ttl="1h"),
            ModelPromptTextBlock(text=_RULES, cache_ttl="1h"),
        ),
        "messages": (
            ModelPromptMessage(
                role="user",
                blocks=(
                    ModelPromptTextBlock(text=_DIFF, cache_ttl="5m"),
                    ModelPromptTextBlock(text=persona),
                ),
            ),
        ),
        "tool_schema": _TOOL,
        "tool_choice": {"type": "tool", "name": "submit_verdict"},
    }


def _request(
    persona: str = "Focus on correctness.", **over: Any
) -> ModelLlmDelegationCallRequest:
    fields: dict[str, Any] = {
        "request_id": "r1",
        "correlation_id": "c1",
        "causation_id": "c1",
        "model_id": _MODEL,
        "endpoint_ref": _NATIVE_URL,
        "prompt_hash": "h",
        "task_type": "code_review",
        "timeout_seconds": 30.0,
        "model_tier": "cloud",
        "provider": "anthropic",
        "secret_ref": _REF,
        "temperature": 1.0,
        "inference_protocol": "anthropic_messages",
        **_blocks(persona),
    }
    fields.update(over)
    return ModelLlmDelegationCallRequest(**fields)


def _call(request: ModelLlmDelegationCallRequest) -> Any:
    return effect.HandlerLlmDelegationCall()(request)


class TestPromptCacheOnTheWire:
    def test_second_persona_reads_the_diff_from_cache(
        self, api: RecordedMessagesApi
    ) -> None:
        first = _call(_request("Focus on correctness."))
        second = _call(_request("Focus on security and error handling instead."))

        assert first.success
        assert second.success
        # The first call wrote the prefix; nothing was there to read.
        assert first.cache_read_input_tokens == 0
        assert first.cache_creation_input_tokens > 0
        assert first.cache_creation_1h_input_tokens > 0
        assert first.cache_creation_5m_input_tokens > 0
        # The second differs only in the persona block, so everything before it
        # (tool, both system blocks, the diff) is served from the cache.
        prefix = (
            _words(json.dumps(_TOOL, sort_keys=True))
            + _words(_INSTRUCTIONS + _CATALOG)
            + _words(_RULES)
            + _words(_DIFF)
        )
        assert second.cache_read_input_tokens == prefix
        assert second.cache_read_input_tokens >= _words(_DIFF)
        assert second.cache_creation_input_tokens == 0
        # tokens_in stays the whole prompt: read + written + uncached.
        assert second.tokens_in == second.cache_read_input_tokens + (
            second.tokens_in - second.cache_read_input_tokens
        )
        assert second.tokens_in > second.cache_read_input_tokens

    def test_wire_layout_puts_the_persona_last_and_uncached(
        self, api: RecordedMessagesApi
    ) -> None:
        _call(_request("Focus on correctness."))
        endpoint, payload, _headers = api.calls[0]
        assert endpoint == _NATIVE_URL
        assert [b.get("cache_control") for b in payload["system"]] == [
            {"type": "ephemeral", "ttl": "1h"},
            {"type": "ephemeral", "ttl": "1h"},
        ]
        diff, persona = payload["messages"][0]["content"]
        assert diff["cache_control"] == {"type": "ephemeral", "ttl": "5m"}
        assert "cache_control" not in persona
        assert persona["text"] == "Focus on correctness."
        assert payload["tools"] == [_TOOL]
        assert payload["tool_choice"] == {"type": "tool", "name": "submit_verdict"}
        assert "response_format" not in payload

    def test_key_goes_in_x_api_key_never_as_a_bearer(
        self, api: RecordedMessagesApi
    ) -> None:
        _call(_request())
        _, _, headers = api.calls[0]
        assert headers["x-api-key"] == _KEY
        assert headers["anthropic-version"] == "2023-06-01"
        assert "Authorization" not in headers

    def test_provider_headers_ride_along_and_never_a_credential(
        self, api: RecordedMessagesApi
    ) -> None:
        _call(_request(provider_headers={"anthropic-workspace-id": "wrkspc_1"}))
        assert api.calls[0][2]["anthropic-workspace-id"] == "wrkspc_1"
        with pytest.raises(ValidationError, match="non-secret"):
            _request(provider_headers={"X-Api-Key": "nope"})

    def test_tool_input_is_the_content_and_usage_is_priced_with_cache_multipliers(
        self, api: RecordedMessagesApi
    ) -> None:
        cold = _call(_request("Focus on correctness."))
        warm = _call(_request("Focus on something else."))
        assert json.loads(cold.content) == {"done": True, "more": False, "f": []}
        assert cold.stop_reason is EnumAnthropicStopReason.TOOL_USE
        assert cold.finish_reason is EnumProviderFinishReason.TOOL_CALLS
        assert cold.served_model_id == _MODEL
        # Reading the prefix at 0.1x and not writing it makes the warm call
        # cheaper than the cold one that wrote it at 1.25x and 2x.
        assert Decimal("0") < warm.actual_cost_usd < cold.actual_cost_usd


class TestStopReasonReachesTheParser:
    def test_max_tokens_is_a_typed_stop_reason_the_parser_refuses(
        self, api: RecordedMessagesApi
    ) -> None:
        api.queued = {
            "id": "msg_02",
            "type": "message",
            "role": "assistant",
            "model": _MODEL,
            "content": [
                {"type": "text", "text": '{"done":true,"more":false,"f":[{"c":"B1'}
            ],
            "stop_reason": "max_tokens",
            "usage": {"input_tokens": 900, "output_tokens": 4096},
        }
        result = _call(_request(tool_schema=None, tool_choice=None))

        assert result.success
        assert result.stop_reason is EnumAnthropicStopReason.MAX_TOKENS
        assert isinstance(result.stop_reason, EnumAnthropicStopReason)
        assert result.finish_reason is EnumProviderFinishReason.LENGTH
        parsed = parse_compact_strict(
            result.content or "",
            source_model=_MODEL,
            stop_reason=result.stop_reason.value,
            known_codes={"B1": "bug"},
            standing_rules={},
            max_findings=10,
        )
        assert not parsed.findings
        assert parsed.failure_kind is EnumParseFailureKind.TRUNCATED

    def test_a_normal_end_is_not_refused_by_the_parser(
        self, api: RecordedMessagesApi
    ) -> None:
        result = _call(_request())
        assert result.stop_reason is EnumAnthropicStopReason.TOOL_USE
        parsed = parse_compact_strict(
            result.content or "",
            source_model=_MODEL,
            stop_reason=result.stop_reason.value,
            known_codes={},
            standing_rules={},
            max_findings=10,
        )
        # The stop signal is a normal end; whatever the parser says next is
        # about the reply's shape, never a cut.
        assert parsed.failure_kind is not EnumParseFailureKind.TRUNCATED

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("end_turn", EnumAnthropicStopReason.END_TURN),
            ("refusal", EnumAnthropicStopReason.REFUSAL),
            (None, EnumAnthropicStopReason.ABSENT),
            ("brand_new_reason", EnumAnthropicStopReason.UNRECOGNISED),
            (7, EnumAnthropicStopReason.UNRECOGNISED),
        ],
    )
    def test_parse_keeps_absent_and_unrecognised_apart(
        self, raw: object, expected: EnumAnthropicStopReason
    ) -> None:
        assert EnumAnthropicStopReason.parse(raw) is expected

    def test_an_empty_content_array_is_invalid_json_not_a_success(
        self, api: RecordedMessagesApi
    ) -> None:
        api.queued = {"content": [], "stop_reason": "end_turn", "usage": {}}
        result = _call(_request())
        assert not result.success
        assert result.error_message == "API returned empty content array"

    def test_the_finished_observation_carries_stop_reason_and_cache_usage(
        self, api: RecordedMessagesApi, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[ModelLlmDelegationCallObservation] = []
        token = effect.current_call_observer.set(seen.append)
        try:
            _call(_request())
            _call(_request("Other persona."))
        finally:
            effect.current_call_observer.reset(token)
        finished = [o for o in seen if o.phase == "finished"]
        assert [o.stop_reason for o in finished] == [
            EnumAnthropicStopReason.TOOL_USE
        ] * 2
        assert finished[0].cache_creation_input_tokens > 0
        assert finished[1].cache_read_input_tokens > 0
        # The review cost record is built from the same observation's figures.
        assert finished[0].cache_write_5m_tokens is not None
        assert finished[0].cache_write_1h_tokens is not None
        assert (
            finished[0].cache_write_5m_tokens + finished[0].cache_write_1h_tokens
            == finished[0].cache_creation_input_tokens
        )
        assert finished[1].cache_read_tokens == finished[1].cache_read_input_tokens


class TestCallsNamingNoPlanKeepTheCompatibilityRow:
    def test_catalogue_resolves_the_compatibility_row_without_a_plan(self) -> None:
        default = resolve_byok_provider_backend("anthropic")
        native = resolve_byok_provider_backend("anthropic", "api_native")
        assert default is not None
        assert native is not None
        assert default.plan == "api"
        assert default.default_plan is True
        assert default.endpoint_url == _CHAT_URL
        assert default.inference_protocol == "openai_chat"
        assert native.plan == "api_native"
        assert native.default_plan is False
        assert native.endpoint_url == _NATIVE_URL
        assert native.inference_protocol == "anthropic_messages"
        assert native.backend_id != default.backend_id

    def test_a_call_naming_no_protocol_posts_the_chat_shape_with_a_bearer(
        self, api: RecordedMessagesApi, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sent: list[tuple[str, dict[str, Any], dict[str, str]]] = []

        def chat(
            *,
            endpoint_url: str,
            payload: dict[str, Any],
            extra_headers: dict[str, str],
            **_: Any,
        ) -> transport.ModelTransportResponse:
            sent.append((endpoint_url, payload, extra_headers))
            return transport.ModelTransportResponse(
                status_code=200,
                json_body={
                    "choices": [
                        {"message": {"content": "ok"}, "finish_reason": "stop"}
                    ],
                    "usage": {"prompt_tokens": 3, "completion_tokens": 1},
                },
                latency_ms=1,
            )

        monkeypatch.setattr(transport, "post_chat_completion", chat)
        monkeypatch.setattr(effect, "_get_served_model_ids", lambda _: None)
        request = ModelLlmDelegationCallRequest(
            request_id="r1",
            correlation_id="c1",
            causation_id="c1",
            model_id=_MODEL,
            endpoint_ref=_CHAT_URL,
            prompt="hello",
            prompt_hash="h",
            system_prompt="be brief",
            timeout_seconds=30.0,
            secret_ref=_REF,
        )
        assert request.inference_protocol == "openai_chat"
        result = _call(request)

        assert result.success
        assert result.content == "ok"
        assert result.stop_reason is None
        assert result.cache_read_input_tokens == 0
        endpoint, payload, headers = sent[0]
        assert endpoint == _CHAT_URL
        assert [m["role"] for m in payload["messages"]] == ["system", "user"]
        assert headers == {"Authorization": f"Bearer {_KEY}"}
        assert api.calls == []


class TestOneRepresentationPerCall:
    def test_blocks_refuse_a_prompt_and_a_system_prompt(self) -> None:
        with pytest.raises(ValidationError, match="one representation"):
            _request(prompt="also a prompt")
        with pytest.raises(ValidationError, match="one representation"):
            _request(system_prompt="also a system prompt")

    def test_native_fields_need_the_native_protocol(self) -> None:
        with pytest.raises(ValidationError, match="anthropic_messages"):
            _request(inference_protocol="openai_chat", endpoint_ref=_CHAT_URL)

    def test_response_format_is_refused_on_the_native_protocol(self) -> None:
        with pytest.raises(ValidationError, match="response_format"):
            _request(response_format={"type": "json_object"})

    def test_tool_choice_needs_a_tool(self) -> None:
        with pytest.raises(ValidationError, match="tool_choice needs tool_schema"):
            _request(tool_schema=None)

    def test_more_than_four_breakpoints_are_refused(self) -> None:
        many = tuple(
            ModelPromptTextBlock(text=f"block {i}", cache_ttl="5m") for i in range(5)
        )
        with pytest.raises(ValidationError, match="at most 4"):
            _request(system_blocks=many)

    def test_a_plain_prompt_on_the_native_protocol_is_one_user_message(
        self, api: RecordedMessagesApi
    ) -> None:
        request = ModelLlmDelegationCallRequest(
            request_id="r1",
            correlation_id="c1",
            causation_id="c1",
            model_id=_MODEL,
            endpoint_ref=_NATIVE_URL,
            prompt="hello",
            prompt_hash="h",
            system_prompt="be brief",
            timeout_seconds=30.0,
            secret_ref=_REF,
            inference_protocol="anthropic_messages",
        )
        _call(request)
        payload = api.calls[0][1]
        assert payload["system"] == "be brief"
        assert payload["messages"] == [{"role": "user", "content": "hello"}]


class TestGatewayServesTheNativeCallEndToEnd:
    """The voter surface, the gateway and the real call effect over the recorded API."""

    def test_two_voter_calls_differing_in_the_persona_report_cache_reads(
        self, api: RecordedMessagesApi
    ) -> None:
        import asyncio

        from omnimarket.nodes.node_claude_review_gateway_effect.handlers.handler_claude_review_gateway import (
            BLOCK_BOUNDARY_MARKER,
            ClaudeReviewGateway,
        )
        from omnimarket.nodes.node_claude_review_gateway_effect.models.model_claude_review_gateway import (
            ModelClaudeReviewGatewayRequest,
        )

        ref = "cred_house_anthropic_key-a"

        class Catalog:
            def query(
                self, table: str, filters: dict[str, object] | None = None
            ) -> list[dict[str, object]]:
                return [
                    {
                        "api_key_ref": ref,
                        "tenant_id": "house",
                        "provider": "anthropic",
                        "name": "key-a",
                        "bound_host": "h201",
                        "revoked_at": None,
                        "expires_at": None,
                    }
                ]

        class Values:
            async def get_secret(self, key: str) -> str | None:
                return _KEY

        gateway = ClaudeReviewGateway(
            ModelClaudeReviewGatewayRequest(
                host="h201",
                tenant_id="house",
                model=_MODEL,
                cloud_refused_repositories=("acme/refused",),
            ),
            catalog=Catalog(),
            key_values=Values(),
            call=effect.HandlerLlmDelegationCall(),
            endpoint_url=_NATIVE_URL,
        )
        marker = BLOCK_BOUNDARY_MARKER

        def review(persona: str) -> dict[str, Any]:
            return {
                "messages": [
                    {
                        "role": "system",
                        "content": f"{_INSTRUCTIONS}{_CATALOG}\n{marker}\n{_RULES}",
                    },
                    {"role": "user", "content": f"{_DIFF}\n{marker}\n{persona}"},
                ],
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "submit_verdict",
                        "schema": _TOOL["input_schema"],
                    },
                },
                "max_tokens": 512,
                "metadata": {"repository": "acme/allowed"},
            }

        status1, first = asyncio.run(gateway.complete(review("correctness")))
        status2, second = asyncio.run(gateway.complete(review("security")))

        assert (status1, status2) == (200, 200)
        assert first["usage"]["cache_read_input_tokens"] == 0
        assert first["usage"]["cache_creation_input_tokens"] > 0
        # Everything up to and including the diff is read from the cache.
        assert second["usage"]["cache_read_input_tokens"] > _words(_DIFF)
        assert (
            second["usage"]["prompt_tokens_details"]["cached_tokens"]
            == (second["usage"]["cache_read_input_tokens"])
        )
        assert second["usage"]["cache_creation_input_tokens"] == 0
        assert second["choices"][0]["finish_reason"] == "stop"
        assert json.loads(second["choices"][0]["message"]["content"])["done"] is True
        # One native call per review, on the key's x-api-key.
        assert [c[0] for c in api.calls] == [_NATIVE_URL, _NATIVE_URL]
        assert all(c[2]["x-api-key"] == _KEY for c in api.calls)
        # The tool is the first element of the cached prefix.
        assert api.calls[1][1]["tools"][0]["name"] == "submit_verdict"
