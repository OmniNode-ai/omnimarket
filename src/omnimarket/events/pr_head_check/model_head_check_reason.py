# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ModelHeadCheckReason: the merge-check reason code of one non-green check."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.merge_control.reason_code_classifier import EnumMergeCheckReasonCode


class ModelHeadCheckReason(BaseModel):
    """A non-green check's name and its per-check reason code.

    The code is the existing merge-check classifier's, never a second
    vocabulary.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(..., min_length=1, description="Check-run name.")
    reason_code: EnumMergeCheckReasonCode = Field(
        ..., description="Per-check reason code from the merge-check classifier."
    )


__all__: list[str] = ["ModelHeadCheckReason"]
