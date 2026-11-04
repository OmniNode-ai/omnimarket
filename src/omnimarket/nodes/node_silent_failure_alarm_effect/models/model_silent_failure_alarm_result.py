# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The correlated tick terminal."""

from uuid import UUID

from pydantic import BaseModel, ConfigDict

from .model_watcher_recovery import ModelWatcherRecoveryResult


class ModelSilentFailureAlarmResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    request_id: UUID
    exit_code: int
    # The one stdout line of the tick; empty when the tick refused before observing anything.
    summary: str
    # Lines the tick writes to stderr.
    warnings: tuple[str, ...] = ()
    # The alarms raised this tick (not deduplicated away): placement-share, tick-liveness.
    alarms: tuple[str, ...] = ()
    # The FRICTION rows the tick rendered for those alarms, stamped.
    rows: tuple[str, ...] = ()
    watcher_recovery: ModelWatcherRecoveryResult | None = None
