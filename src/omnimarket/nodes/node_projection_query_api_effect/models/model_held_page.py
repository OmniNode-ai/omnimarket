# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A projection page read back from the page cache, with how long it has been held (OMN-24458)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ModelHeldPage(BaseModel):
    """One page the shared Valkey held, and its age since the runtime that computed it stored it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    page: dict[str, Any]
    age_seconds: float = Field(
        ge=0,
        description="Seconds since the page was stored; never negative, a store clock ahead of the reader's reads as 0.",
    )


__all__ = ["ModelHeldPage"]
