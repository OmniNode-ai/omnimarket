# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed models of a host reading: a verdict read from one host's own state, at one moment.

A piece reading (``model_reading``) is a pure function of ledger rows in a closed window. A host
reading has no window: the state it reads exists only on the host and only now, so every run has
its own row key. The reading writer can take the rows as a source through its overlay.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.model_reading import EnumReadingOutcome, EnumReadingVerdict


class EnumHostProbe(StrEnum):
    LEDGER_OUTBOX = "ledger-outbox"
    LEDGER_OLD_NAMES = "ledger-old-names"
    #: Append one probe row through the bus and read it back from the durable store by a deadline.
    LEDGER_DURABILITY = "ledger-durability"


class ModelProbeReading(BaseModel):
    """What one probe read: the verdict, why, and the measured cells for the row."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    probe: EnumHostProbe
    verdict: EnumReadingVerdict
    outcome: EnumReadingOutcome
    #: Token-safe reason codes, never blank.
    reasons: tuple[str, ...] = Field(min_length=1)
    cells: dict[str, str] = Field(default_factory=dict)


class ModelHostReadingCommand(BaseModel):
    """Read this host's declared probes now."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(min_length=1)
    #: Only these probes; empty means every probe the overlay declares for the host.
    probes: tuple[EnumHostProbe, ...] = ()
    #: A probe run reads and computes and appends nothing.
    write: bool = True


class ModelHostProbeOutcome(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    probe: EnumHostProbe
    host: str
    verdict: EnumReadingVerdict
    outcome: EnumReadingOutcome
    reasons: tuple[str, ...]
    row_key: str
    #: ``written``, ``already-written``, ``not-written`` (a probe) or ``append-failed:<why>``.
    ledger: str
    row: str | None = None

    @property
    def failed(self) -> bool:
        """The reading itself failed; a NOT_WORKING verdict that was read is not a failure."""
        return self.outcome is not EnumReadingOutcome.READ or self.ledger.startswith(
            "append-failed"
        )


class ModelHostReadingResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    status: Literal["completed", "failed"]
    host: str | None = None
    failure: str | None = None
    probes: tuple[ModelHostProbeOutcome, ...] = ()
