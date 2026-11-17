# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay the stall responder scheduled effect reads: cadence, windows, redispatch bound and ledger route."""

from __future__ import annotations

from typing import Annotated

from pydantic import ConfigDict, Field, model_validator

from omnimarket.models.schedule_trigger.model_schedule_trigger import (
    ModelScheduleDeclaration,
)
from omnimarket.nodes.node_reading_writer_effect.overlay import (
    ModelCommandLedgerReadRoute,
    ModelFileLedgerReadRoute,
)


class ModelStallResponderScheduleOverlay(ModelScheduleDeclaration):
    """What each run reads: the stall window, the open-claim look-back, the redispatch bound and the ledger route.

    window_seconds bounds the stall rows a run reads. claim_lookback_seconds bounds the CLAIM and TERMINAL rows it reads
    to find the lanes still open, and is at least the window: a lane claimed before the window and not closed is live.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    window_seconds: int = Field(gt=0)
    claim_lookback_seconds: int = Field(gt=0)
    redispatch_after_hours: float = Field(gt=0)
    ledger_read: Annotated[
        ModelFileLedgerReadRoute | ModelCommandLedgerReadRoute,
        Field(discriminator="route"),
    ]

    @model_validator(mode="after")
    def _lookback_covers_the_window(self) -> ModelStallResponderScheduleOverlay:
        if self.claim_lookback_seconds < self.window_seconds:
            raise ValueError("claim_lookback_seconds must be at least window_seconds")
        return self


__all__: list[str] = ["ModelStallResponderScheduleOverlay"]
