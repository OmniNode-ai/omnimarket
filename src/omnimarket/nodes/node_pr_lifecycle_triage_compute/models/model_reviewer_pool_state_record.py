# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The controller state's record of the heads one PR had its reviewer red rerun at."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ModelReviewerPoolStateRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    pr: str
    rerun_heads: tuple[str, ...] | None = None


__all__: list[str] = ["ModelReviewerPoolStateRecord"]
