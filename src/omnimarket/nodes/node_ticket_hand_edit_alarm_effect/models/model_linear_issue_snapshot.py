# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What one Linear read returns for an issue, and for one revision of its description."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ModelLinearIssueSnapshot(BaseModel):
    """An issue as Linear returns it now."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticket_id: str = Field(
        min_length=1, description="The identifier, for example OMN-1."
    )
    issue_uuid: str = Field(min_length=1)
    title: str
    description: str
    updated_at: datetime


class ModelDescriptionRevision(BaseModel):
    """One entry of documentContentHistory."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    snapshot_at: datetime
    actor_ids: tuple[str, ...] = ()
