# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared event/command models for design-to-plan downstream routing."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ModelPlanToTicketsStartCommand(BaseModel):
    """Command payload for onex.cmd.omnimarket.plan-to-tickets-start.v1."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = ""
    plan_path: str
    project: str = ""
    epic_title: str = ""
    no_create_epic: bool = False
    dry_run: bool = False
    skip_existing: bool = False
    team: str = "Omninode"
    repo: str = ""
    allow_arch_violation: bool = False


class ModelPlanToTicketsCompletedEvent(BaseModel):
    """Terminal event for node_plan_to_tickets."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: str = "completed"
    correlation_id: str = ""
    created_count: int = 0
    skipped_count: int = 0
    failed_count: int = 0
    epic_id: str = ""


__all__ = [
    "ModelPlanToTicketsCompletedEvent",
    "ModelPlanToTicketsStartCommand",
]
