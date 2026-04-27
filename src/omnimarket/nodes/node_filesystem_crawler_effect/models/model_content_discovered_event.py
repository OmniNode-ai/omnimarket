# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ModelContentDiscoveredEvent — emitted per file discovered by the content-type-aware crawler."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field


class ModelContentDiscoveredEvent(BaseModel):
    """Event emitted when a file is discovered during a content-type-aware crawl."""

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    event_id: UUID = Field(default_factory=uuid4)
    correlation_id: UUID
    emitted_at_utc: datetime = Field(default_factory=lambda: datetime.now(UTC))
    source_ref: str
    content_type: str
    content_fingerprint: str
    file_size_bytes: int = Field(..., ge=0)
    mtime: float
    root_path: str
    relative_path: str


__all__ = ["ModelContentDiscoveredEvent"]
