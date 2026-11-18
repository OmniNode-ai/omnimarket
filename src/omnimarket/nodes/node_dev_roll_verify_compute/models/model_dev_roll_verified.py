# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The verdict: how many runtime starts one finished dev roll actually made."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.nodes.node_dev_roll_verify_compute.models.enum_dev_roll_verdict import (
    EnumDevRollVerdict,
)
from omnimarket.nodes.node_dev_roll_verify_compute.models.model_dev_branch_install import (
    ModelDevBranchInstall,
)


class ModelDevRollVerified(BaseModel):
    """One roll verdict against the declared starts-per-roll bound.

    ``starts_counted`` is ``None`` only for ``start_count_unknown``, when the
    start events could not be read; every other verdict counts them.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    observed_at: datetime
    job_id: str
    verdict: EnumDevRollVerdict
    starts_counted: int | None = Field(ge=0)
    declared_starts: int
    reapply_starts: int = Field(ge=0)
    starts_by_cause: dict[str, int]
    declared_branch_installs: tuple[ModelDevBranchInstall, ...]
    first_start_at: datetime | None
    last_start_at: datetime | None
    detail: str

    @model_validator(mode="after")
    def _verdict_matches_starts_counted(self) -> ModelDevRollVerified:
        if self.verdict is EnumDevRollVerdict.START_COUNT_UNKNOWN:
            if self.starts_counted is not None:
                raise ValueError("start_count_unknown needs starts_counted unset")
        elif self.starts_counted is None:
            raise ValueError(f"{self.verdict.value} needs a counted start count")
        elif self.verdict is EnumDevRollVerdict.PASSED:
            if self.starts_counted != self.declared_starts:
                raise ValueError("passed needs starts_counted equal to declared_starts")
        elif self.verdict is EnumDevRollVerdict.TOO_MANY_STARTS:
            if self.starts_counted <= self.declared_starts:
                raise ValueError(
                    "too_many_starts needs starts_counted above declared_starts"
                )
        elif self.starts_counted >= self.declared_starts:
            raise ValueError("no_start needs starts_counted below declared_starts")
        return self
