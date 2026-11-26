# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request, registry row and result of the tools/list handler."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EnumMcpRegistrationState(StrEnum):
    """Registration states the registry reader reports for a node."""

    PENDING_REGISTRATION = "pending_registration"
    ACCEPTED = "accepted"
    AWAITING_ACK = "awaiting_ack"
    ACK_RECEIVED = "ack_received"
    ACTIVE = "active"
    LIVENESS_EXPIRED = "liveness_expired"
    REJECTED = "rejected"
    ACK_TIMED_OUT = "ack_timed_out"


class ModelMcpToolEntry(BaseModel):
    """One exposed operation, the fields of a contract ``mcp.tools`` entry.

    The registry reader builds it from the contract's typed mcp block, so this
    node reads the entry without importing the contract model.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool_name: str
    description: str
    timeout_seconds: int = Field(..., gt=0)
    input_model: str
    output_model: str
    command_topic: str
    completed_topic: str
    failed_topic: str


class ModelMcpRegistryRow(BaseModel):
    """One registered node: its state and its contract's mcp exposure."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    node_name: str = Field(..., min_length=1)
    state: EnumMcpRegistrationState
    expose: bool = False
    tools: tuple[ModelMcpToolEntry, ...] = ()


class ModelMcpOverlay(BaseModel):
    """Typed overlay of the server; the allow list is default deny."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    allowed_tools: frozenset[str] = Field(default_factory=frozenset)


class ModelMcpToolsListRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    overlay: ModelMcpOverlay


class ModelMcpToolListing(BaseModel):
    """One listed tool: the contract's entry plus the node that owns it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool_name: str
    node_name: str
    description: str
    timeout_seconds: int
    input_model: str
    output_model: str
    command_topic: str
    completed_topic: str
    failed_topic: str


class ModelMcpToolsListResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    tools: tuple[ModelMcpToolListing, ...] = ()
