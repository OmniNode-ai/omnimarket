# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lab chat completion post for the memory pilot's baseline arm (OMN-23660).

The node declares ``transport_type: kafka`` and a node's ``metadata.transport_type`` holds one value, so the httpx
exception import lives here, behind the handler, instead of widening that declaration (the shape of
``node_provider_rung_canary_effect/adapters/adapter_provider_rung_post.py``). The post goes through the sanctioned
delegation transport; this adapter only turns its transport failures into the pilot's typed error.
"""

from __future__ import annotations

from typing import Any

from omnimarket.nodes.node_llm_delegation_call_effect.handlers.transport import (
    post_chat_completion,
)
from omnimarket.nodes.node_memory_pilot_effect.protocols.protocol_memory_pilot import (
    LabModelUnavailableError,
)


def chat_completion_text(
    *, endpoint_url: str, payload: dict[str, Any], timeout_seconds: float
) -> str:
    """The first choice's message text, or ``LabModelUnavailableError`` naming the failure class."""
    import httpx

    try:
        response = post_chat_completion(
            endpoint_url=endpoint_url,
            payload=payload,
            timeout_seconds=timeout_seconds,
        )
        content = response.json_body["choices"][0]["message"]["content"]
    except (httpx.HTTPError, OSError, ValueError, KeyError, IndexError) as exc:
        raise LabModelUnavailableError(type(exc).__name__) from None
    return (content or "").strip()
