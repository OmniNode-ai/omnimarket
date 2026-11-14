# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay the work-ledger reducer scheduled effect reads: which ledger window to reduce."""

from __future__ import annotations

from pydantic import ConfigDict, Field

from omnimarket.models.schedule_trigger.model_schedule_trigger import (
    ModelScheduleDeclaration,
)


class ModelWorkLedgerReducerScheduleOverlay(ModelScheduleDeclaration):
    """The bounded window of ledger rows the reducer reads and reduces on each due tick."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ledger_name: str = Field(min_length=1)
    ledger_id: str = Field(min_length=1)
    read_command_env: str = Field(min_length=1)
    read_timeout_seconds: int = Field(ge=1)
    window_seconds: int = Field(ge=60)
    row_limit: int = Field(ge=1)
    prior_lookback_seconds: int = Field(ge=0)
    prior_kinds: tuple[str, ...] = Field(min_length=1)


__all__: list[str] = [
    "ModelWorkLedgerReducerScheduleOverlay",
]
