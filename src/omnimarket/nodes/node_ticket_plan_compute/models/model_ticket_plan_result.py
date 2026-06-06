# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Output models for node_ticket_plan_compute."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelTicketSpec(BaseModel):
    """A single ticket definition parsed from a plan document."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    title: str = Field(description="Ticket title")
    description: str = Field(default="", description="Ticket body / description")
    phase: str | None = Field(
        default=None, description="Plan phase this ticket belongs to"
    )
    depends_on: list[str] = Field(
        default_factory=list,
        description="Titles of tickets this one depends on",
    )
    labels: list[str] = Field(default_factory=list, description="Label names to apply")


class ModelTicketPlanResult(BaseModel):
    """Result of the ticket plan computation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tickets: list[ModelTicketSpec] = Field(
        default_factory=list,
        description="Structured ticket definitions parsed from the plan",
    )
    parse_warnings: list[str] = Field(
        default_factory=list,
        description="Non-fatal parse warnings",
    )
