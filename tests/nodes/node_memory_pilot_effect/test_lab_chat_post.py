# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lab chat post: a good reply is text, every transport or reply-shape failure is the typed lab_model_unavailable (OMN-23660)."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from omnimarket.nodes.node_llm_delegation_call_effect.handlers.transport import (
    ModelTransportResponse,
)
from omnimarket.nodes.node_memory_pilot_effect.adapters import adapter_lab_chat_post
from omnimarket.nodes.node_memory_pilot_effect.handlers.lab_answerer import (
    LabChatAnswerer,
)
from omnimarket.nodes.node_memory_pilot_effect.models.model_memory_pilot import (
    ModelLabEndpoint,
)
from omnimarket.nodes.node_memory_pilot_effect.protocols.protocol_memory_pilot import (
    LabModelUnavailableError,
)

pytestmark = pytest.mark.unit

ENDPOINT = ModelLabEndpoint(
    base_url="http://lab.example.test:8000/v1", model="m", timeout_seconds=5
)


def _reply(body: Any) -> ModelTransportResponse:
    return ModelTransportResponse(status_code=200, json_body=body, latency_ms=1)


def _answer() -> str:
    answerer = LabChatAnswerer(
        ENDPOINT,
        temperature=0.0,
        max_completion_tokens=16,
        llm_args={"extra_body": {"chat_template_kwargs": {"enable_thinking": False}}},
    )
    return asyncio.run(answerer.answer("sys", "q?", "ctx"))


def test_a_good_reply_is_its_stripped_text(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def post(**kw: Any) -> ModelTransportResponse:
        seen.update(kw)
        return _reply({"choices": [{"message": {"content": "  yes \n"}}]})

    monkeypatch.setattr(adapter_lab_chat_post, "post_chat_completion", post)
    assert _answer() == "yes"
    assert seen["endpoint_url"] == "http://lab.example.test:8000/v1/chat/completions"
    assert seen["payload"]["extra_body"] == {
        "chat_template_kwargs": {"enable_thinking": False}
    }


@pytest.mark.parametrize(
    "failure",
    [
        httpx.ConnectError("refused"),
        httpx.ReadTimeout("slow"),
        ConnectionRefusedError("refused"),
    ],
)
def test_a_transport_failure_is_the_typed_lab_error(
    monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    def post(**kw: Any) -> ModelTransportResponse:
        raise failure

    monkeypatch.setattr(adapter_lab_chat_post, "post_chat_completion", post)
    with pytest.raises(LabModelUnavailableError) as raised:
        _answer()
    assert type(failure).__name__ in str(raised.value)


def test_a_reply_without_choices_is_the_typed_lab_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failing control: a reply of the wrong shape must not become an invented answer."""

    def post(**_: Any) -> ModelTransportResponse:
        return _reply({})

    monkeypatch.setattr(adapter_lab_chat_post, "post_chat_completion", post)
    with pytest.raises(LabModelUnavailableError):
        _answer()
