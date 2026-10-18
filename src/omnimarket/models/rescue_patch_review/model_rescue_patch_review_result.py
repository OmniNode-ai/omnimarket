# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The verdict, its reason and the changed files."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from .enum_rescue_patch_verdict import EnumRescuePatchVerdict


class ModelRescuePatchReviewResult(BaseModel):
    """The verdict, its reason and the changed files."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    verdict: EnumRescuePatchVerdict
    reason: str
    files: tuple[str, ...]
