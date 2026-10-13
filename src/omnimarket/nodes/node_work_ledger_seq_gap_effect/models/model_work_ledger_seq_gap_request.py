# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A read-only sequence gap examination."""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ModelWorkLedgerSeqGapRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    ledger_id: str = "rolling-work-ledger"
    from_seq: int = Field(default=1, ge=1)
    since: datetime | None = None
    until: datetime | None = None
    expected_max_seq: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _window(self) -> ModelWorkLedgerSeqGapRequest:
        if (self.since is None) != (self.until is None):
            raise ValueError("since and until must be supplied together")
        for value in (self.since, self.until):
            if value is not None and value.utcoffset() != timedelta(0):
                raise ValueError("window timestamps must be timezone-aware UTC")
        if (
            self.since is not None
            and self.until is not None
            and self.since > self.until
        ):
            raise ValueError("since must not be after until")
        return self
