# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One row of the plan's graph table, as the contract generator reads it (OMN-21883)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelPlanTargetRow(BaseModel):
    """The cells of one plan table row that become a contract row, plus its prose.

    ``check`` and ``action`` are the plan's own words for the already-satisfied
    check and the action. The contract keeps neither: the generator classifies
    them into the closed enums, and uses the words only to refuse a
    classification they contradict.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    number: int = Field(ge=1)
    name: str = Field(min_length=1)
    stage: str = Field(pattern=r"^[A-Z]$")
    needs: tuple[str, ...]
    check: str = Field(min_length=1)
    action: str = Field(min_length=1)
    falsifier: str = Field(min_length=1)


__all__ = ["ModelPlanTargetRow"]
