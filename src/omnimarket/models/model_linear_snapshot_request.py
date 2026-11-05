# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared request for the daily Linear snapshot writer and its tick caller."""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

NonEmpty = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class ModelLinearSnapshotRequest(BaseModel):
    """One write: the directory to write into and the UTC date in the file name (``None``: today)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    out_dir: NonEmpty
    date: Annotated[str, StringConstraints(pattern=r"^\d{4}-\d{2}-\d{2}$")] | None = (
        None
    )
    page_size: int = Field(default=100, ge=1, le=250)
    max_pages: int = Field(default=60, ge=1)
