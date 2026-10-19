# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Client-call identity carried on a delegate-skill command (plan TB.3, OMN-21098).

A client call is one model-raised ``Agent`` call that the Claude Code adapter
offers to delegation. Its identity is the client, the session and the tool use,
all drawn from the hook input and never fabricated: a call whose ids are absent
carries no ``client_call`` at all.
"""

from __future__ import annotations

import hashlib
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.claude_code_binding import EnumClientMaxMode


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


__all__: list[str] = ["ModelDelegateSkillClientCall"]
