# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The tenant savings headline, in the v1 shape the post-login panel reads."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict


class ModelTenantSavings(BaseModel):
    """Totals over one tenant's projection rows; figures are real aggregates only."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    total_saved_usd: float
    total_spend_usd: float
    net_saved_usd: float
    delegation_runs: int
    currency: str
    as_of: str
    data_state: Literal["ok", "insufficient_data"]
