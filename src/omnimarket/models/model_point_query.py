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

    query: Literal["inbox", "holds", "open-claims", "open-rulings", "rows"]
    lanes: tuple[str, ...] = ()
    lane: str | None = None
    #: The ticket whose open rulings an ``open-rulings`` read lists (OMN-19609).
    ticket: str | None = None
    since: datetime | None = None
    #: The window's exclusive end; the node answers up to its own now when none is sent (OMN-23606).
    until: datetime | None = None
    #: A case-insensitive substring the row's first line must hold; the node narrows in the store (OMN-23606).
    term: str | None = Field(default=None, min_length=1)
    kinds: tuple[str, ...] = ()
    limit: int | None = Field(default=None, ge=1)
    page_size: int | None = Field(default=None, ge=1)
    cursor: str | None = None
    #: A paged rows answer carries lines and no typed rows; the reader prints lines only.
    lines_only: bool = False
    #: An ``open-claims`` read lists only the claims stamped at or after this time (the node's own claims_since); the open set before it is not sent.
    claims_since: datetime | None = None
