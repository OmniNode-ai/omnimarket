# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed models of the start-path census and refusal drill (OMN-23306, step 0 row 4, rebuild plan W28)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.model_reading import EnumReadingOutcome, EnumReadingVerdict


class ModelStartPathDrillCommand(BaseModel):
    """Run the drill once; ``write`` appends the reading row and the observe-log rows."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(min_length=1)
    write: bool = True


class ModelPathDrilled(BaseModel):
    """What the drill did on one census path."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str
    binding: str
    #: The planted start (a piece under a stop) was refused: proceed false.
    planted_refused: bool
    #: The control start (a piece the policy admits) was admitted: proceed true.
    control_admitted: bool
    planted_outcome: str
    control_outcome: str


class ModelStartPathDrillResult(BaseModel):
    """The one reading of a drill run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    status: Literal["completed", "failed"]
    host: str
    failure: str = ""
    verdict: EnumReadingVerdict = EnumReadingVerdict.UNKNOWN
    outcome: EnumReadingOutcome = EnumReadingOutcome.SOURCE_UNREADABLE
    reasons: tuple[str, ...] = ()
    #: Paths the census holds, the paths it was required to hold and does not, and its bindings.
    census_paths: int = 0
    census_missing: tuple[str, ...] = ()
    bound: int = 0
    unbound: int = 0
    dead: int = 0
    #: Planted starts refused, planted starts that got through (a bypass), control starts admitted.
    refused: int = 0
    admitted: int = 0
    control_admitted: int = 0
    paths: tuple[ModelPathDrilled, ...] = ()
    #: Observe-log rows that reached the log and rows that did not.
    log_written: int = 0
    log_failed: int = 0
    row_key: str = ""
    ledger: str = "not-written"
    row: str = ""

    @property
    def failed(self) -> bool:
        return self.status == "failed" or self.ledger.startswith("append-failed")
