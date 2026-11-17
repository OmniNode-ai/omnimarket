# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay the stall responder scheduled effect reads: cadence, look-back, redispatch bound and ledger route."""

from __future__ import annotations

from typing import Annotated

from pydantic import ConfigDict, Field

from omnimarket.models.schedule_trigger.model_schedule_trigger import (
    ModelScheduleDeclaration,
)
from omnimarket.nodes.node_reading_writer_effect.overlay import (
    ModelCommandLedgerReadRoute,
    ModelFileLedgerReadRoute,
)


class ModelStallResponderScheduleOverlay(ModelScheduleDeclaration):
    """What each run reads: the look-back window, the redispatch bound and the ledger route."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    window_seconds: int = Field(gt=0)
    redispatch_after_hours: float = Field(gt=0)
    ledger_read: Annotated[
        ModelFileLedgerReadRoute | ModelCommandLedgerReadRoute,
        Field(discriminator="route"),
    ]


__all__: list[str] = ["ModelStallResponderScheduleOverlay"]
