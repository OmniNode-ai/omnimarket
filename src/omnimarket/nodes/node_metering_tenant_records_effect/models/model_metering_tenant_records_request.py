# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The request for one tenant's metering records (OMN-26449)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class ModelMeteringTenantRecordsRequest(BaseModel):
    """Which tenant's records to read, from which store and window.

    ``tenant_id`` is the tenant as the caller names it: its canonical UUID, a non-canonical UUID string, or a slug.
    ``db_path`` None reads the canonical local evidence store. The window is ``[window_start, window_end)``; either
    bound may be None.
    """

    model_config = ConfigDict(
        frozen=True, extra="forbid", arbitrary_types_allowed=False
    )

    tenant_id: str = Field(..., min_length=1)
    db_path: Path | None = None
    window_start: datetime | None = None
    window_end: datetime | None = None
    include_fixtures: bool = False


__all__ = ["ModelMeteringTenantRecordsRequest"]
