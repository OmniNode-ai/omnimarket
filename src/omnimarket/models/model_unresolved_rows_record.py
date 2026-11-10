# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The file the unresolved step writes and the factory floor reads (OMN-19998)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from omnimarket.models.model_factory_floor_input import ModelFloorLedgerRow


class ModelUnresolvedRowsRecord(BaseModel):
    """Rows of the live ledger and every archive roll with no age limit, read once by their own step.

    ``covers_before`` is the edge the step read up to: every matching row with a time before it is in ``rows``. It lies
    ``valid_hours`` past the window's edge, so the record stays whole for that long after ``read_at``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    read_at: datetime
    covers_before: datetime
    valid_hours: int = Field(gt=0)
    seconds: float = Field(ge=0)
    rows: tuple[ModelFloorLedgerRow, ...]

    @field_validator("read_at", "covers_before")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("a record time carries a UTC offset")
        return value
