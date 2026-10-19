# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The native Anthropic Messages wire format for the delegation call effect (OMN-20910).

``HandlerLlmDelegationCall`` posts one request and reads one reply. For the
``anthropic_messages`` protocol this module builds the request body from the
typed call request and parses the reply into a typed reading; the handler keeps
the transport, the error classification and the telemetry, so there is one
sanctioned model caller and not two.

The request is described as blocks so the prompt cache can hold a prefix:
tools, then system, then messages. A block with ``cache_ttl`` ends a cached
prefix. The reply carries the provider's own ``stop_reason`` and its measured
cache usage, which are returned typed and never inferred.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from omnimarket.enums.enum_anthropic_stop_reason import EnumAnthropicStopReason
from omnimarket.events.llm_delegation_call import ModelLlmDelegationCallRequest
from omnimarket.models.delegation.prompt_text_block import (
    ModelPromptMessage,
    ModelPromptTextBlock,
)
from omnimarket.nodes.node_llm_delegation_call_effect.models.model_anthropic_messages_reading import (
    ModelAnthropicMessagesReading,
)

ANTHROPIC_VERSION = "2023-06-01"
#: The credential header of the native API. The key is attached at the effect
#: boundary only, never carried on a request.
API_KEY_HEADER = "x-api-key"
VERSION_HEADER = "anthropic-version"


def _cache_control(block: ModelPromptTextBlock) -> dict[str, Any]:
    if block.cache_ttl is None:
        return {}
    return {"cache_control": {"type": "ephemeral", "ttl": block.cache_ttl}}


def _text_block(block: ModelPromptTextBlock) -> dict[str, Any]:
    return {"type": "text", "text": block.text, **_cache_control(block)}


def _message(message: ModelPromptMessage) -> dict[str, Any]:
    return {
        "role": message.role,
        "content": [_text_block(block) for block in message.blocks],
    }


def build_messages_payload(request: ModelLlmDelegationCallRequest) -> dict[str, Any]:
    """The ``/v1/messages`` body for ``request``.

    Blocks are sent as given, in order; a plain ``prompt`` and ``system_prompt``
    become one user message and one system string. ``provider_request_options``
    are merged last, as on the chat path.
    """
    payload: dict[str, Any] = {
        "model": request.model_id,
        "max_tokens": request.max_tokens,
        "temperature": request.temperature,
    }
    if request.messages:
        payload["messages"] = [_message(m) for m in request.messages]
        if request.system_blocks:
            payload["system"] = [_text_block(b) for b in request.system_blocks]
    else:
        payload["messages"] = [{"role": "user", "content": request.prompt}]
        if request.system_prompt:
            payload["system"] = request.system_prompt
    if request.tool_schema is not None:
        payload["tools"] = [request.tool_schema]
    if request.tool_choice is not None:
        payload["tool_choice"] = request.tool_choice
    if request.provider_request_options:
        payload.update(request.provider_request_options)
    return payload


def native_headers(
    request: ModelLlmDelegationCallRequest, api_key: str | None
) -> dict[str, str]:
    """Request headers: the static ones, the non-secret provider ones, the key."""
    headers = {VERSION_HEADER: ANTHROPIC_VERSION}
    headers.update(request.extra_headers)
    headers.update(request.provider_headers)
    if api_key is not None:
        headers[API_KEY_HEADER] = api_key
    return headers


def _int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _cache_creation(usage: Mapping[str, Any]) -> tuple[int, int, int]:
    """(total, 5-minute, 1-hour) tokens written to the cache by this call.

    The provider reports the total, and where it can the split by TTL under
    ``cache_creation``. A total with no split is a 5-minute write: that is the
    default TTL a breakpoint without a TTL gets, and attributing it to the
    cheaper multiplier is the conservative reading of an unsplit number.
    """
    total = _int(usage.get("cache_creation_input_tokens"))
    detail = usage.get("cache_creation")
    if isinstance(detail, Mapping):
        five = _int(detail.get("ephemeral_5m_input_tokens"))
        hour = _int(detail.get("ephemeral_1h_input_tokens"))
        if five + hour > 0:
            return max(total, five + hour), five, hour
    return total, total, 0


def _blocks_text(content: Sequence[Any]) -> tuple[str, str]:
    """(text, source_field) of a reply: a tool's input if one was used, else the text."""
    for block in content:
        if isinstance(block, Mapping) and block.get("type") == "tool_use":
            tool_input = block.get("input")
            return json.dumps(tool_input, separators=(",", ":")), "content[].input"
    text = "".join(
        str(block.get("text", ""))
        for block in content
        if isinstance(block, Mapping) and block.get("type") == "text"
    )
    return text, "content[].text"


def parse_messages_response(
    body: Mapping[str, Any],
) -> ModelAnthropicMessagesReading | None:
    """Read a ``/v1/messages`` reply, or ``None`` when it carries no content array."""
    content = body.get("content")
    if not isinstance(content, list) or not content:
        return None
    text, source_field = _blocks_text(content)
    usage_raw = body.get("usage")
    usage: Mapping[str, Any] = usage_raw if isinstance(usage_raw, Mapping) else {}
    created, created_5m, created_1h = _cache_creation(usage)
    served = body.get("model")
    return ModelAnthropicMessagesReading(
        content=text,
        source_field=source_field,
        stop_reason=EnumAnthropicStopReason.parse(body.get("stop_reason")),
        served_model=served if isinstance(served, str) and served else None,
        input_tokens=_int(usage.get("input_tokens")),
        output_tokens=_int(usage.get("output_tokens")),
        cache_creation_5m_input_tokens=created_5m,
        cache_creation_1h_input_tokens=created_1h,
        cache_creation_input_tokens=created,
        cache_read_input_tokens=_int(usage.get("cache_read_input_tokens")),
    )


__all__ = [
    "ANTHROPIC_VERSION",
    "API_KEY_HEADER",
    "VERSION_HEADER",
    "ModelAnthropicMessagesReading",
    "build_messages_payload",
    "native_headers",
    "parse_messages_response",
]
