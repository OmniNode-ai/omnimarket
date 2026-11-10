# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared typed point-read request carried to the work-ledger query node."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["ModelPointQuery"]


class ModelPointQuery(BaseModel):
    """The typed request the read port carries to the query node."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    query: Literal["inbox", "holds", "open-claims", "rows"]
    lanes: tuple[str, ...] = ()
    lane: str | None = None
    since: datetime | None = None
    kinds: tuple[str, ...] = ()
    limit: int | None = Field(default=None, ge=1)
    page_size: int | None = Field(default=None, ge=1)
    cursor: str | None = None
