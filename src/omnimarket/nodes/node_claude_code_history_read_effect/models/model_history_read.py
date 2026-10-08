# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request and result of the Claude Code history read (OMN-19979)."""

from __future__ import annotations

from enum import StrEnum

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from omnimarket.models.savings_estimate import ModelClaudeCodePromptRecord


class EnumHistoryReadStatus(StrEnum):
    """How a read ended. Only READ carries records."""

    READ = "read"
    #: opt_in was not true; the source was never opened.
    REFUSED_NO_OPT_IN = "refused_no_opt_in"
    #: source_root does not exist.
    SOURCE_MISSING = "source_missing"


class ModelClaudeCodeHistoryReadRequest(BaseModel):
    """Read the developer's own history, only when they said so."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    opt_in: bool = Field(
        default=False,
        description="Must be true. History is never read by default.",
    )
    source_root: str = Field(..., min_length=1)
    project_filter: str | None = None
    since: AwareDatetime | None = None
    until: AwareDatetime | None = None


class ModelClaudeCodeHistoryReadResult(BaseModel):
    """Records in (occurred_at, session_id, prompt_id) order."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: EnumHistoryReadStatus
    records: tuple[ModelClaudeCodePromptRecord, ...] = ()
    files_read: int = Field(default=0, ge=0)
    lines_skipped: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _only_a_read_carries_records(self) -> ModelClaudeCodeHistoryReadResult:
        if self.status is not EnumHistoryReadStatus.READ and (
            self.records or self.files_read
        ):
            raise ValueError(f"a {self.status.value} result carries no records")
        return self
