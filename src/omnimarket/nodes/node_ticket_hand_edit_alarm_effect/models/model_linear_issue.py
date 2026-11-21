# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What the detector reads from Linear: a changed issue and its description revisions."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ModelLinearIssueSnapshot(BaseModel):
    """One issue as the list read returned it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    identifier: str = Field(..., min_length=1)
    title: str
    description: str
    updated_at: datetime
    document_content_id: str | None = None


class ModelDescriptionRevision(BaseModel):
    """One revision of an issue's description document (``documentContentHistory``)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    snapshot_at: datetime
    actor_ids: tuple[str, ...] = ()
