# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One answer field's pass or fail inside a scored item (OMN-24170)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ModelEvalFieldScore(BaseModel):
    """The name of one scored field and whether it passed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    passed: bool


__all__ = ["ModelEvalFieldScore"]
