# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Whether an order edge fired after the head's red was graded."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ModelFiredEdgeVerdict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    stale: bool


__all__: list[str] = ["ModelFiredEdgeVerdict"]
