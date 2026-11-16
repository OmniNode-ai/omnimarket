# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Model the overlay home validates overlays/lane_inbox_guard/overlay.yaml through (OMN-24110).

The lane inbox guard (a plain python hook) reads the same document through its own
configuration; no value lives here.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ModelLaneInboxGuardOverlay(BaseModel):
    """The lane inbox guard overlay document."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    reader: str = Field(min_length=1)
    reader_module: str = Field(min_length=1)
    interpreter_env: str = Field(min_length=1)
    lane_env: str = Field(min_length=1)
    state_file_name: str = Field(min_length=1)
    journal_file_name: str = Field(min_length=1)
    cache_file_name: str = Field(min_length=1)
    refresh_log_name: str = Field(min_length=1)
    refresher_script: str = Field(min_length=1)
    fallback_inbox_script: str = Field(min_length=1)
    hold_push_pattern: str = Field(min_length=1)
    outbox_env: str = Field(min_length=1)
    outbox_append_script: str = Field(min_length=1)
    outbox_delivered_file_name: str = Field(min_length=1)
    window_hours: int = Field(gt=0, strict=True)
    interval_s: int = Field(gt=0, strict=True)
    timeout_s: int = Field(gt=0, strict=True)
    cold_wait_s: int = Field(gt=0, strict=True)
    max_listed: int = Field(gt=0, strict=True)
    outbox_timeout_s: int = Field(gt=0, strict=True)
    outbox_kinds: tuple[str, ...] = Field(min_length=1)
    skip_hold_cells: tuple[str, ...] = ()

    @field_validator("hold_push_pattern")
    @classmethod
    def _validate_hold_push_pattern(cls, pattern: str) -> str:
        """Refuse a pattern that does not compile as a regular expression."""
        try:
            re.compile(pattern)
        except re.error as error:
            raise ValueError(
                f"hold_push_pattern {pattern!r} does not compile: {error}"
            ) from error
        return pattern

    @field_validator("outbox_kinds", "skip_hold_cells")
    @classmethod
    def _validate_non_empty_cells(cls, cells: tuple[str, ...]) -> tuple[str, ...]:
        """Refuse an empty cell in either list."""
        for cell in cells:
            if not cell:
                raise ValueError("each cell must be a non-empty string")
        return cells


LANE_INBOX_GUARD_OVERLAY_KEYS: tuple[str, ...] = tuple(
    ModelLaneInboxGuardOverlay.model_fields
)

__all__ = [
    "LANE_INBOX_GUARD_OVERLAY_KEYS",
    "ModelLaneInboxGuardOverlay",
]
