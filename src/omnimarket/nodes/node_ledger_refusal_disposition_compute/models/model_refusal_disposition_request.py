# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Immutable snapshots; the compute performs no filesystem or bus access."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, field_validator


class ModelRefusalDispositionRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)
    artifacts: tuple[tuple[str, bytes | None], ...]
    canonical_rows: tuple[str, ...]
    now: datetime

    @field_validator("now")
    @classmethod
    def require_aware_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("check time must be timezone aware")
        return value
