# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The duplicate guard's claims read, as its snapshot contract declares it (OMN-26433)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__ = ["ModelLedgerClaimsReadConfig"]


class ModelLedgerClaimsReadConfig(BaseModel):
    """What the guard reads through the ledger reader node, and how long each read may wait.

    The primary read is two narrow reads of the same ledger: every row of the last ``heartbeat_window_hours`` (a lane's
    heartbeat is any of its rows) and the ``claim_kinds`` rows of ``window_hours`` (the rows that open or close a CLAIM).
    Together they give ``live_claims`` the same answer as every row of ``window_hours`` at a few pages in place of
    many. The fallback is that whole-window read. Each tier waits at most its own timeout, and together they never
    wait more than ``total_budget_s``. When neither answers the guard is unchecked.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    reader: str = Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")
    window_hours: int = Field(ge=1, le=24 * 30)
    heartbeat_window_hours: int = Field(ge=1)
    claim_kinds: tuple[str, ...] = Field(min_length=1)
    primary_timeout_s: float = Field(gt=0)
    fallback_timeout_s: float = Field(gt=0)
    total_budget_s: float = Field(gt=0)

    @model_validator(mode="after")
    def _tiers_fit_the_budget(self) -> ModelLedgerClaimsReadConfig:
        if self.heartbeat_window_hours > self.window_hours:
            raise ValueError("the heartbeat window is inside the claims window")
        if self.primary_timeout_s + self.fallback_timeout_s > self.total_budget_s:
            raise ValueError("the two tiers' waits must fit total_budget_s")
        return self
