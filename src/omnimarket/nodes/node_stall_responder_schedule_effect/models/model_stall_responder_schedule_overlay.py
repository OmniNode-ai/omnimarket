# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay the stall responder schedule reads: its cadence, the look-back and the ledger route."""

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
    """How often the stall rows are read, how far back, and where the ledger is read from."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    window_seconds: int = Field(gt=0)
    ledger_read: Annotated[
        ModelFileLedgerReadRoute | ModelCommandLedgerReadRoute,
        Field(discriminator="route"),
    ]


__all__: list[str] = ["ModelStallResponderScheduleOverlay"]
