# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Client-call identity carried on a delegate-skill command (plan TB.3, OMN-21098).

A client call is one model-raised ``Agent`` call that the Claude Code adapter
offers to delegation. Its identity is the client, the session and the tool use,
all drawn from the hook input and never fabricated: a call whose ids are absent
carries no ``client_call`` at all.

The identity is narrower than the delivery identity the orchestrator already
uses: ``client_call_key`` names the tool call, ``request_fingerprint`` names the
request content, and the shared correlation id is derived from both, so a
command that reuses a key with a different request gets a different correlation
id and its terminal can never reach the original caller (OMN-21099). The
namespace of the derived id is
``uuid5(NAMESPACE_URL, "onex:omnimarket:delegate-skill:client-call")``; a client
that derives the id itself must use the same value.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.claude_code_binding import EnumClientMaxMode

if TYPE_CHECKING:
    from omnimarket.models.delegation.wire.model_delegate_skill_request import (
        ModelDelegateSkillRequest,
    )

CLIENT_CALL_CORRELATION_NAMESPACE = uuid.uuid5(
    uuid.NAMESPACE_URL, "onex:omnimarket:delegate-skill:client-call"
)

# Keys left out of the canonical request: they differ between two sends of the
# same call and carry no request content.
_FINGERPRINT_EXCLUDED_KEYS = ("correlation_id", "published_at", "trace_id", "span_id")

_CLIENT_MODE_RANK: dict[EnumClientMaxMode, int] = {
    EnumClientMaxMode.OBSERVE: 0,
    EnumClientMaxMode.SELECTIVE: 1,
    EnumClientMaxMode.REQUIRED: 2,
}


class ModelDelegateSkillClientCall(BaseModel):
    """Who raised the call and the highest mode that client allows itself."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    client: Literal["claude-code", "codex", "external-client"] = Field(
        ...,
        description="Registered adapter source; matches the request's source.",
    )
    session_id: str = Field(..., min_length=1, description="Client session id.")
    tool_use_id: str = Field(..., min_length=1, description="Client tool-use id.")
    client_host: str = Field(
        ...,
        min_length=1,
        description="Host name the adapter reports; routing data, not identity.",
    )
    client_max_mode: EnumClientMaxMode = Field(
        ...,
        description="Highest mode the client's installed contract allows this host.",
    )

    @property
    def client_call_key(self) -> str:
        """``sha256(client:session_id:tool_use_id)``, the client call's identity."""
        material = f"{self.client}:{self.session_id}:{self.tool_use_id}"
        return hashlib.sha256(material.encode("utf-8")).hexdigest()


def client_mode_rank(mode: EnumClientMaxMode) -> int:
    """Order of a client mode: observe < selective < required."""
    return _CLIENT_MODE_RANK[mode]


def lesser_client_mode(
    first: EnumClientMaxMode, second: EnumClientMaxMode
) -> EnumClientMaxMode:
    """The lower of two modes; the effective mode is the client max vs the cohort."""
    return first if client_mode_rank(first) <= client_mode_rank(second) else second


def request_fingerprint(request: ModelDelegateSkillRequest) -> str:
    """sha256 of the canonical request, minus correlation, timestamps and trace ids."""
    body = request.model_dump(mode="json", exclude_none=True)
    for key in _FINGERPRINT_EXCLUDED_KEYS:
        body.pop(key, None)
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def client_call_correlation_id(
    client_call: ModelDelegateSkillClientCall, request: ModelDelegateSkillRequest
) -> uuid.UUID:
    """``uuid5(NS, key + ":" + fingerprint)`` for one client call and its request."""
    return uuid.uuid5(
        CLIENT_CALL_CORRELATION_NAMESPACE,
        f"{client_call.client_call_key}:{request_fingerprint(request)}",
    )


__all__: list[str] = [
    "CLIENT_CALL_CORRELATION_NAMESPACE",
    "ModelDelegateSkillClientCall",
    "client_call_correlation_id",
    "client_mode_rank",
    "lesser_client_mode",
    "request_fingerprint",
]
