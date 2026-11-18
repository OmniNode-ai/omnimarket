# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Outbound event on onex.evt.omniintelligence.intent-classified.v1.

The field names are the ones the intent-classification projection reads
(``projection_intent_classification``, UPSERT by ``correlation_id``), so the
event needs no alias on the consuming side. ``event_type`` keeps the value the
retired omniintelligence publisher used, for any reader keyed on it.

``correlation_id`` and ``emitted_at`` stay ``str`` on the wire (bytes
unchanged) but are validated: the correlation id must parse as a UUID and the
emission time as an ISO 8601 datetime, so the event only ever carries values
its consumers accept.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ModelIntentClassified(BaseModel):
    """One classified prompt."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_type: Literal["IntentClassified"] = "IntentClassified"
    session_id: str
    correlation_id: str
    intent_class: str
    intent_category: str
    confidence: float = Field(..., ge=0.0, le=1.0)
    keywords: list[str] = Field(default_factory=list)
    emitted_at: str
    agent_source: str = "claude"

    @field_validator("correlation_id")
    @classmethod
    def _correlation_id_is_uuid(cls, value: str) -> str:
        """The correlation id must parse as a UUID (consumers upsert on it)."""
        UUID(value)
        return value

    @field_validator("emitted_at")
    @classmethod
    def _emitted_at_is_iso8601(cls, value: str) -> str:
        """The emission time must parse as an ISO 8601 datetime (trailing Z ok)."""
        text = value
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        datetime.fromisoformat(text)
        return value


__all__ = ["ModelIntentClassified"]
