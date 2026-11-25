# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The duplicate guard's claims read, as its snapshot contract declares it (OMN-26433)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__ = ["ModelLedgerClaimsReadConfig"]


class ModelLedgerClaimsReadConfig(BaseModel):
    """What the guard reads through the ledger reader node, and how long each read may wait.

    The read is two narrow reads of the same ledger, one after the other: every row of the last
    ``heartbeat_window_hours`` (a lane's heartbeat is any of its rows; they name the live lanes), then the ``claim_kinds`` rows
    of ``window_hours`` for those lanes (the rows that open or close their CLAIMs). Together they give ``live_claims`` the same
    answer as every row of ``window_hours`` at two pages in place of dozens. A read that fails is asked once more, the same
    read, with ``retry_timeout_s``: the every-row read of ``window_hours`` is never a fallback, because it needs dozens of
    reply pages and does not finish inside any bound the guard can wait. A first read waits at most ``primary_timeout_s``, and
    every read of the guard together never waits more than ``total_budget_s``. When a read and its retry both fail the guard
    is unchecked.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    reader: str = Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")
    window_hours: int = Field(ge=1, le=24 * 30)
    heartbeat_window_hours: int = Field(ge=1)
    claim_kinds: tuple[str, ...] = Field(min_length=1)
    primary_timeout_s: float = Field(gt=0)
    retry_timeout_s: float = Field(gt=0)
    total_budget_s: float = Field(gt=0)

    @model_validator(mode="after")
    def _tiers_fit_the_budget(self) -> ModelLedgerClaimsReadConfig:
        if self.heartbeat_window_hours > self.window_hours:
            raise ValueError("the heartbeat window is inside the claims window")
        if 2 * (self.primary_timeout_s + self.retry_timeout_s) > self.total_budget_s:
            raise ValueError(
                "the waits of the two reads and of their retries must fit total_budget_s"
            )
        return self
