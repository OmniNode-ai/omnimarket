# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The event shape the fold reads: tolerant of the typed event and the classifier's (OMN-26706)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

_FIELDS = (
    "kind",
    "input_id",
    "head_sha",
    "evidence",
    "action_id",
    "observed_at",
    "ci_read_at",
    "worker_tier",
    "lane_name",
    "red_kind",
    "deadline_attempt",
    "request_id",
    "reason",
    "decision_needed",
)


class ModelLaneWorkFoldInput(BaseModel):
    """One input to the fold. `cls` and `deadline` are the names the classifier uses."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: str
    input_id: str | None = None
    head_sha: str | None = None
    evidence: tuple[str, ...] = ()
    cls: str | None = None
    action_id: str | None = None
    observed_at: datetime | None = None
    ci_read_at: datetime | None = None
    worker_tier: str | None = None
    lane_name: str | None = None
    red_kind: str | None = None
    deadline: str | None = None
    deadline_attempt: int | None = Field(default=None, ge=0)
    request_id: str | None = None
    help_origin: str | None = None
    reason: str | None = None
    decision_needed: str | None = None

    @classmethod
    def coerce(cls, event: Any) -> ModelLaneWorkFoldInput:
        """Read any event object: the typed core event, the classifier's, or this model."""
        if isinstance(event, cls):
            return event
        data: dict[str, Any] = {}
        for name in _FIELDS:
            value = getattr(event, name, None)
            if value is not None:
                data[name] = getattr(value, "value", value)
        for target, names in (
            ("cls", ("cls", "event_class")),
            ("deadline", ("deadline", "deadline_id")),
            ("help_origin", ("help_origin",)),
        ):
            for name in names:
                value = getattr(event, name, None)
                if value is not None:
                    data[target] = getattr(value, "value", value)
                    break
        return cls(**data)


__all__ = ["ModelLaneWorkFoldInput"]
