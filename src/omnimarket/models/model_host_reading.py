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
    HOST_CANARY = "host-canary"
    #: Join J1: the code graph's verdict for the nodes host discovery and placement depend on.
    JOIN_J1 = "join-j1"


class EnumCanaryEnding(StrEnum):
    """How one host's canary lane ended (piece 2 of the base-first plan).

    ``typed`` and ``busy`` are typed endings: the lane started and ended with its result, or the
    placement refused it with its own typed refusal. Every other ending is a failure of the host:
    a wrapper that was killed or a result that cannot be read is ``untyped``, a canary cut at its
    deadline is ``timed-out`` (never free capacity), a host the pool read could not reach is
    ``unreachable``.
    """

    TYPED = "typed"
    BUSY = "busy"
    UNTYPED = "untyped"
    TIMED_OUT = "timed-out"
    UNREACHABLE = "unreachable"


class ModelCanaryHost(BaseModel):
    """One row of the lab host table as the canary reads it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    pooled: bool
    #: Why the table parks the host; empty for a pooled host.
    reason: str = ""


class ModelCanaryRun(BaseModel):
    """What the route returned for one canary: exit code and output, or the deadline."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    exit_code: int | None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False


class ModelCanaryHostReading(BaseModel):
    """One pooled host's canary: its ending, the disk it read and the slots the pool read gave."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str = Field(min_length=1)
    ending: EnumCanaryEnding
    free_disk_gb: float | None = None
    slots: int | None = None
    #: Token-safe reasons this host fails the reading; empty when it passes.
    failures: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return not self.failures


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
    #: The measured cells of the row (per-host endings for the host canary).
    cells: dict[str, str] = Field(default_factory=dict)
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
