# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The shared transition publication seam for the lab job projection."""

from __future__ import annotations

from typing import Self

from pydantic import BaseModel, ConfigDict, model_validator

from omnimarket.models.lab_job.model_lab_job_reduce import ModelLabJobTransition
from omnimarket.models.lab_job.model_lab_job_row import ModelLabJobRow


class ModelLabJobTransitioned(BaseModel):
    """The post-transition row and the transition carrying its CAS-assigned seq."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    row: ModelLabJobRow
    transition: ModelLabJobTransition

    @model_validator(mode="after")
    def _same_job_and_seq(self) -> Self:
        if self.row.job_id != self.transition.job_id:
            raise ValueError("row.job_id must equal transition.job_id")
        if self.row.seq != self.transition.seq:
            raise ValueError("row.seq must equal transition.seq")
        return self


__all__: list[str] = ["ModelLabJobTransitioned"]
