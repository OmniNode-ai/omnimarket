# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What would be sent to the deploy agent, in the field names of its command."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, field_validator


class ModelDevRollRequest(BaseModel):
    """Field names are those of the agent's ModelRebuildRequested (omnibase_infra).

    The seam test pins them against a list spelled in this repository.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    requested_by: str
    scope: str
    runtime_lane: str
    git_ref: str
    requested_at: datetime

    @field_validator("requested_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("requested_at must carry a UTC offset")
        return value

    def wire_payload(self) -> dict[str, object]:
        """The JSON-mode payload the agent's command model would validate."""
        return self.model_dump(mode="json")
