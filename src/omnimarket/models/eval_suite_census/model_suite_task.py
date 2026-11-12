# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One suite task of the code eval census and its bound checks (OMN-23883)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.models.delegation.model_size_band import EnumSizeBand
from omnimarket.nodes.node_code_contract_score_effect.models import (
    EnumCodeScoreSource,
    EnumCodeScoreVerdict,
)

_COMMIT_PATTERN = r"^[0-9a-f]{40}$"


class ModelCodeEvalSuiteCheck(BaseModel):
    """One runnable check bound to a suite task by the task's contract."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    item_id: str = Field(min_length=1)
    check_type: str = Field(min_length=1)
    check_value: str = Field(min_length=1)


class ModelCodeEvalSuiteTask(BaseModel):
    """One already-merged ticket the suite measures the local models on.

    ``control`` is the verdict the merged scorer's control step returned: the
    checks fail at the base commit and are expected to pass with the merged
    change. Only an ``unscorable`` control names a ``control_source``; a pass
    or a ``fail_*`` verdict carries none.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    repository: str = Field(min_length=1)
    merge_commit: str = Field(pattern=_COMMIT_PATTERN)
    base_commit: str = Field(pattern=_COMMIT_PATTERN)
    contract_id: str = Field(pattern=r"^OMN-[0-9]+$")
    subject: str = Field(min_length=1)
    merged_at: str = Field(min_length=1)
    files_changed: int = Field(ge=1)
    lines_changed: int = Field(ge=1)
    band: EnumSizeBand
    checks: tuple[ModelCodeEvalSuiteCheck, ...] = Field(min_length=1)
    check_files: tuple[str, ...] = Field(min_length=1)
    held_paths: tuple[str, ...] = Field(min_length=1)
    control: EnumCodeScoreVerdict
    control_source: EnumCodeScoreSource | None = None

    @model_validator(mode="after")
    def _validate_control_source(self) -> ModelCodeEvalSuiteTask:
        if self.control == EnumCodeScoreVerdict.UNSCORABLE:
            if self.control_source is None:
                raise ValueError("an unscorable control must name its control_source")
            return self
        if self.control_source is not None:
            raise ValueError("only an unscorable control may carry a control_source")
        return self


__all__ = [
    "ModelCodeEvalSuiteCheck",
    "ModelCodeEvalSuiteTask",
]
