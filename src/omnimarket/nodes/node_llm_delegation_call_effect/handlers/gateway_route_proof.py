# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One-token native request through an owned local ONEX gateway (OMN-23413).

Setup proves a freshly started gateway answers before it binds Claude Code to
it. The request goes to the gateway this install owns, which routes it through
canonical routing and writes the run receipt; this module only sends it. It
lives in the delegation call effect because that is the node sanctioned to send
a model request (the direct-model-call policy).
"""

from __future__ import annotations

import json
from typing import cast

import httpx
from pydantic import JsonValue


def prove_gateway_route(
    url: str, token: str, model: str
) -> tuple[dict[str, JsonValue], str]:
    """Return the gateway's answer and the run id it minted for the proof request."""
    with httpx.Client(trust_env=False, timeout=60) as client:
        response = client.post(
            url + "/v1/messages",
            headers={"authorization": "Bearer " + token},
            json={
                "model": model,
                "max_tokens": 8,
                "messages": [{"role": "user", "content": "Reply with hello."}],
            },
        )
        response.raise_for_status()
        body = json.loads(response.content)
    if not isinstance(body, dict):
        raise ValueError("Native model proof did not return an object")
    return cast("dict[str, JsonValue]", body), response.headers.get("x-onex-run-id", "")
