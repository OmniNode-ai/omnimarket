# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ledger-safe portion of either lab work terminal (OMN-20278)."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator


class ModelWorkLedgerBusMirrorRequest(BaseModel):
    """Normalize both terminal schemas, including receipts from older hosts.

    Log paths, output tails and error messages are deliberately not ledger cells.
    The source envelope's timestamp is supplied by the serve subscription.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    work_unit_id: str = Field(min_length=1, max_length=64)
    host: str = Field(min_length=1, max_length=255)
    lane: str = Field(default="", max_length=255)
    repo: str = Field(default="", max_length=255)
    commit_sha: str = Field(default="", max_length=40)
    kind: Literal["test", "build", "lint", "code_draft", "other"] = "other"
    status: Literal["completed", "failed", "timed_out", "refused", "infra_error"]
    exit_code: int | None = None
    duration_seconds: float = Field(default=0.0, ge=0, allow_inf_nan=False)
    envelope_timestamp: Annotated[datetime, AwareDatetime]

    @field_validator("work_unit_id", "host", "lane", "repo", "commit_sha")
    @classmethod
    def _safe_cell(cls, value: str) -> str:
        if any(
            character.isspace() or character == "|" or ord(character) < 32
            for character in value
        ):
            raise ValueError("ledger attribution must be a single pipe-free token")
        return value
