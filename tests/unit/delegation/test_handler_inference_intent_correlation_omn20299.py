# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20299: a self-hosted model call carries the run's correlation id.

The model servers' own logs are the check no caller can skip: a request a
server answered either joins a ``delegation_events`` run or it bypassed the
delegation nodes. The .201 vLLM access log records each request's path and
query string, so the id rides in the query string (and in a header for any
log that records headers). The POST URL argument stays ``intent.base_url``
verbatim (OMN-12815); the query parameter is the httpx client's ``params``.
A third-party provider gets neither.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from omnibase_core.models.delegation.wire import ModelInferenceIntent

from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_inference_intent import (
    SELF_HOSTED_CORRELATION_HEADER,
    SELF_HOSTED_CORRELATION_QUERY_PARAM,
    HandlerInferenceIntent,
)

pytestmark = pytest.mark.usefixtures("stub_provider_quota_reader")

_RESPONSE = {
    "id": "chatcmpl-omn20299",
    "choices": [{"message": {"content": "ok"}}],
    "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
}


def _post_kwargs(base_url: str) -> tuple[ModelInferenceIntent, str, dict[str, object]]:
    intent = ModelInferenceIntent(
        base_url=base_url,
        model="Qwen3.8-27B",
        system_prompt="s",
        prompt="p",
        max_tokens=8,
        timeout_seconds=30.0,
        correlation_id=uuid4(),
    )
    captured: dict[str, object] = {}
    response = MagicMock()
    response.json.return_value = _RESPONSE
    response.raise_for_status.return_value = None

    def _capture(url: str, **kwargs: object) -> MagicMock:
        captured["url"] = url
        captured.update(kwargs)
        return response

    # OMN-13501 no-faked-boundary: effect-handler unit test asserts request construction
    # (verbatim POST URL / query parameters / headers) at the egress;
    # RecordedReplayInferenceTransport.calls records only model/url/request_hash, not
    # headers or query parameters. Integrated path proven by the .201 lab proof.
    with patch("httpx.Client") as client_cls:  # onex-allow-faked-boundary
        client = MagicMock()
        client.__enter__ = MagicMock(return_value=client)
        client.__exit__ = MagicMock(return_value=False)
        client.post.side_effect = _capture
        client_cls.return_value = client
        HandlerInferenceIntent().handle(intent)
    captured["params"] = client_cls.call_args.kwargs.get("params")
    url = captured.pop("url")
    assert isinstance(url, str)
    return intent, url, captured


@pytest.mark.unit
@pytest.mark.parametrize(
    "base_url",
    [
        "http://192.168.86.201:8000/v1/chat/completions",  # onex-allow-internal-ip OMN-20299 reason="the self-hosted lab model server the correlation id is for"
        "http://127.0.0.1:8000/v1/chat/completions",
        "http://localhost:8130/v1/chat/completions",
    ],
)
def test_self_hosted_call_carries_the_correlation_id(base_url: str) -> None:
    intent, url, kwargs = _post_kwargs(base_url)

    assert url == base_url
    assert kwargs["params"] == {
        SELF_HOSTED_CORRELATION_QUERY_PARAM: str(intent.correlation_id)
    }
    headers = kwargs["headers"]
    assert isinstance(headers, dict)
    assert headers[SELF_HOSTED_CORRELATION_HEADER] == str(intent.correlation_id)


@pytest.mark.unit
@pytest.mark.parametrize(
    "base_url",
    [
        "https://openrouter.ai/api/v1/chat/completions",
        "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
        "http://8.8.8.8:8000/v1/chat/completions",
    ],
)
def test_third_party_call_carries_no_correlation_id(base_url: str) -> None:
    _intent, url, kwargs = _post_kwargs(base_url)

    assert url == base_url
    assert kwargs.get("params") is None
    headers = kwargs.get("headers") or {}
    assert isinstance(headers, dict)
    assert SELF_HOSTED_CORRELATION_HEADER not in headers


@pytest.mark.unit
def test_query_parameter_name_is_the_one_the_reconciler_reads() -> None:
    """omnibase_internal's model-server reconciler parses exactly this name."""
    assert SELF_HOSTED_CORRELATION_QUERY_PARAM == "onex_cid"
    assert SELF_HOSTED_CORRELATION_HEADER == "X-Onex-Correlation-Id"
