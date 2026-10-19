# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Client-call identity carried by a delegate-skill command.

A client call is one tool call of a client (Claude Code) that asks for delegation.
Its identity is narrower than the delivery identity the orchestrator already
uses: ``client_call_key`` names the tool call, ``request_fingerprint`` names the
request content, and the shared ``correlation_id`` is derived from both, so a
command that reuses a key with a different request gets a different correlation
id and its terminal can never reach the original caller.

The namespace of the derived correlation id is
``uuid5(NAMESPACE_URL, "onex:omnimarket:delegate-skill:client-call")``; a client
that derives the id itself must use the same value.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.enums.enum_delegation_client_mode import EnumDelegationClientMode

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


class ModelDelegateSkillClientCall(BaseModel):
    """Who is calling and how far it may rely on delegation.

    Absent ids mean no ``client_call`` at all (observe-only), never a fabricated
    id, so every id field is required and non-empty.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    client: Literal["claude-code"] = Field(description="Client that made the call.")
    session_id: str = Field(min_length=1)
    tool_use_id: str = Field(min_length=1)
    client_host: str = Field(
        min_length=1, description="Host name the server cohorts are keyed on."
    )
    client_max_mode: EnumDelegationClientMode = Field(
        description="Highest mode the installed client binding allows."
    )


def client_call_key(client_call: ModelDelegateSkillClientCall) -> str:
    """``sha256("<client>:<session_id>:<tool_use_id>")`` as lowercase hex."""
    raw = f"{client_call.client}:{client_call.session_id}:{client_call.tool_use_id}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def request_fingerprint(request: ModelDelegateSkillRequest) -> str:
    """sha256 of the canonical request, minus correlation, timestamps and trace ids."""
    body = request.model_dump(mode="json", exclude_none=True)
    for key in _FINGERPRINT_EXCLUDED_KEYS:
        body.pop(key, None)
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def client_call_correlation_id(request: ModelDelegateSkillRequest) -> uuid.UUID:
    """``uuid5(NS, key + ":" + fingerprint)`` for a request carrying a client call."""
    if request.client_call is None:
        raise ValueError(
            "client_call is required to derive a client-call correlation id"
        )
    key = client_call_key(request.client_call)
    return uuid.uuid5(
        CLIENT_CALL_CORRELATION_NAMESPACE, f"{key}:{request_fingerprint(request)}"
    )


__all__ = [
    "CLIENT_CALL_CORRELATION_NAMESPACE",
    "ModelDelegateSkillClientCall",
    "client_call_correlation_id",
    "client_call_key",
    "request_fingerprint",
]
