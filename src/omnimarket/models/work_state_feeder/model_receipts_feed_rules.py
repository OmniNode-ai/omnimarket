# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The live receipts feeder's feed rules: which directories hold the lane receipts (OMN-23427)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelReceiptsFeedRules(BaseModel):
    """Where the receipts feeder gathers from, and how fresh a receipt must be to be worth gathering.

    The paths are the launching hosts' receipt directories; the projection's own directory it writes the
    gathered receipts into is never a rule here -- that path is the projection contract's.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_dirs: tuple[str, ...] = Field(
        min_length=1,
        description="The directories the lane runner writes receipts into, read in order.",
    )
    source_glob: str = Field(
        description="The glob a gathered receipt matches inside a source directory."
    )
    max_receipt_age_s: int = Field(
        gt=0,
        description="A receipt whose mtime is older than this is left where it lies.",
    )
    gathered_subdir: str = Field(
        description="The subdirectory of the projection's receipts directory the receipts are written into."
    )


__all__ = ["ModelReceiptsFeedRules"]
