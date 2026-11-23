# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lane rows of one window, as the ledger reader node answers them."""

from pydantic import BaseModel, ConfigDict, Field


class ModelPrCreateRefusalAlertRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    window_start: str = Field(
        description="UTC start of the window, as the caller states it."
    )
    window_end: str = Field(
        description="UTC end of the window, as the caller states it."
    )
    rows: tuple[str, ...] = Field(
        description="Ledger rows of the window, one '<stamp> | <TYPE> | lane=<lane> | ...' line each; only STATUS and TERMINAL rows are read."
    )
    already_alerted: tuple[str, ...] = Field(
        default=(),
        description="Causes an alert was already raised for; a cause named here raises no second alert.",
    )
