# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Models for node_audit_trail_compactor."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelAuditEntry(BaseModel):
    """A single audit trail entry (friction event or dispatch log record)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    entry_type: str = Field(
        ...,
        description="Type of entry: friction | dispatch_failure | dispatch_success",
    )
    ticket_id: str | None = Field(default=None, description="Associated ticket ID.")
    agent_id: str | None = Field(default=None, description="Agent or skill ID.")
    description: str = Field(default="", description="Human-readable description.")
    recorded_at: str = Field(..., description="ISO-8601 timestamp.")
