# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Named harness tiers kept separate from the live routing ladder."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelHarnessTier(BaseModel):
    """One harness backend and the task classes it serves."""

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    name: str = Field(..., pattern=r"^harness_")
    backend_id: str = Field(..., min_length=1)
    use_for: tuple[str, ...] = Field(..., min_length=1)


__all__: list[str] = ["ModelHarnessTier"]
