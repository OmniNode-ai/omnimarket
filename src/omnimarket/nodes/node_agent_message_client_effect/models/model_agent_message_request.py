# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What the command line and the MCP tools hand the client handlers.

Fields are raw strings on purpose: the handlers own the checks, so a bad value comes back as
a typed refusal and never as a validation stack trace.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_agent_message_client_effect.models.model_agent_message import (
    ModelAgentMessage,
)


class ModelAgentMessageSendRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    to: str = ""
    thread: str = ""
    kind: str = ""
    platform: str = ""
    text: str = ""
    sender: str | None = None


class ModelAgentMessageReadRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    to: str = ""
    thread: str | None = None
    since: str | None = None
    limit: int = 50


class ModelAgentMessageSendResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    message: ModelAgentMessage


class ModelAgentMessageReadResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    messages: tuple[ModelAgentMessage, ...]
