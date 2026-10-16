# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Input of the external dead-man check (OMN-20808)."""

from __future__ import annotations

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from .model_external_deadman_issue import ModelExternalDeadmanIssue


class ModelExternalDeadmanCheckRequest(BaseModel):
    """The raw text of both mirror variables, the open issues and the clock.

    A variable that is missing or unreadable is ``None`` or its raw text; the handler judges it
    stale rather than failing, because the dead-man exists for the day the mirrors stop.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    now: AwareDatetime
    primary_mirror: str | None
    deadman_mirror: str | None
    open_issues: tuple[ModelExternalDeadmanIssue, ...] = ()
    #: A mirror older than this is stale (plan 4.8: 20 minutes).
    mirror_stale_after_seconds: int = Field(default=1200, gt=0)
    #: Past an episode's delivery-due time, how long before the dead-man escalates it.
    escalation_bound_seconds: int = Field(default=1800, ge=0)
