# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Count a chat request's input tokens with the vLLM server's own tokenizer (OMN-26640).

vLLM's ``POST /tokenize`` takes the same ``messages`` a chat completion takes, applies the served model's
chat template and returns ``count``: the tokens the request will meter as input. The count is the server's
reading, never an estimate. The call is a POST to the server the chat request goes to, built from the
chat-completions URL the routing contract resolved, and it is bounded by the timeout the caller passes.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from typing import Any

_CHAT_SUFFIX = "/v1/chat/completions"
_TOKENIZE_PATH = "/tokenize"

PostJson = Callable[[str, dict[str, Any], float], Any]


class InputTokenMeterError(RuntimeError):
    """The server could not be asked, or answered no usable count."""


def _post_json(url: str, body: dict[str, Any], timeout_s: float) -> Any:
    data = json.dumps(body).encode("utf-8")
    http_request = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(http_request, timeout=timeout_s) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as error:
        raise InputTokenMeterError(f"tokenize call to {url} failed: {error}") from error


class VllmTokenizeMeter:
    """``ProtocolInputTokenMeter`` over a vLLM server's ``/tokenize``."""

    def __init__(self, post: PostJson | None = None) -> None:
        self._post: PostJson = post if post is not None else _post_json

    def count(
        self,
        *,
        endpoint_ref: str,
        model_id: str,
        messages: Sequence[Mapping[str, str]],
        timeout_s: float,
    ) -> int:
        if not endpoint_ref.endswith(_CHAT_SUFFIX):
            raise InputTokenMeterError(
                f"endpoint {endpoint_ref} is not a chat-completions URL"
            )
        url = endpoint_ref.removesuffix(_CHAT_SUFFIX) + _TOKENIZE_PATH
        reply = self._post(
            url, {"model": model_id, "messages": [dict(m) for m in messages]}, timeout_s
        )
        count = reply.get("count") if isinstance(reply, dict) else None
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise InputTokenMeterError(f"{url} answered no token count")
        return count


__all__ = ["InputTokenMeterError", "PostJson", "VllmTokenizeMeter"]
