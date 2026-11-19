# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Settings for the work-ledger projection source of a worktree reconcile command."""

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ModelLedgerProjectionSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    bus_lane: str = Field(min_length=1)
    timeout_seconds: float = Field(gt=0)
    page_size: int = Field(default=200, ge=1, le=1000)
    max_pages: int = Field(default=500, ge=1)
    required_kinds: tuple[str, ...] = Field(
        default=("CLAIM", "TERMINAL", "RELEASE"), min_length=1
    )
    unmeasured_days_allowed: int = Field(default=1, ge=0)


class ModelLedgerSourceChoice(BaseModel):
    """Which ledger a reconcile command reads: the file, or the work_ledger_rows projection.

    The projection is read through the work-ledger query node and used only when
    its parity verdict shows the required kinds complete.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    ledger_source: Literal["file", "projection"] = "file"
    ledger_projection: ModelLedgerProjectionSettings | None = None

    @model_validator(mode="after")
    def _ledger_source_pairs_with_settings(self) -> Self:
        if self.ledger_source == "projection" and self.ledger_projection is None:
            raise ValueError(
                "ledger_source projection requires ledger_projection settings"
            )
        if self.ledger_source == "file" and self.ledger_projection is not None:
            raise ValueError(
                "ledger_projection settings are only used by ledger_source projection"
            )
        return self
