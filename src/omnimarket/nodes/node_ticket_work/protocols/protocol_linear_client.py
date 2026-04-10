# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Protocol interface for Linear API operations used by HandlerTicketWork."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field


class ModelLinearIssue(BaseModel):
    """Minimal Linear issue representation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(..., description="Linear issue ID (e.g., OMN-1234).")
    title: str = Field(default="")
    description: str = Field(default="")
    branch_name: str = Field(default="", description="Linear-suggested branch name.")
    state: str = Field(default="", description="Current workflow state name.")
    team_id: str = Field(default="")


class ModelLinearStateInfo(BaseModel):
    """A workflow state in Linear."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    name: str
    type: str = Field(default="")


@runtime_checkable
class ProtocolLinearClient(Protocol):
    """Protocol for Linear API operations.

    Implementations inject the real MCP-backed client; tests inject stubs.
    """

    def get_issue(self, ticket_id: str) -> ModelLinearIssue:
        """Fetch a Linear issue by ID."""
        ...

    def update_issue_description(self, ticket_id: str, description: str) -> None:
        """Update the description of a Linear issue."""
        ...

    def update_issue_state(self, ticket_id: str, state_name: str) -> bool:
        """Update the workflow state of a Linear issue.

        Returns True if the update succeeded, False if the state was not found.
        """
        ...

    def list_states(self, team_id: str) -> list[ModelLinearStateInfo]:
        """List available workflow states for a team."""
        ...


__all__: list[str] = [
    "ModelLinearIssue",
    "ModelLinearStateInfo",
    "ProtocolLinearClient",
]
