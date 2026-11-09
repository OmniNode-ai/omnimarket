# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed models of the overlay consumer join check: the command, each finding, the run (OMN-23150)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.model_reading import EnumReadingOutcome, EnumReadingVerdict


class ModelOverlayJoinCommand(BaseModel):
    """Run the join check once; ``write`` false reads and appends nothing."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(min_length=1)
    write: bool = True


class ModelConsumerMismatch(BaseModel):
    """A consumer whose effective value differs from the overlay's value.

    ``overlay_value`` and ``consumer_value`` are the values as written; the ``normalized`` pair is each
    in the overlay's base unit, which is what was compared.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    consumer: str
    overlay_value: str
    consumer_value: str
    overlay_normalized: str
    consumer_normalized: str
    #: ``file`` when the consumer's file gave the value, ``env`` when its override variable did.
    consumer_via: Literal["file", "env"]


class ModelParkListFinding(BaseModel):
    """A park list defined in a file outside every overlay."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    scan: str
    line: int
    text: str


class ModelOverlayJoinResult(BaseModel):
    """The run: its verdict, each mismatch and park list found, and what happened on the ledger."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    status: Literal["completed", "failed"]
    host: str
    failure: str | None = None
    verdict: EnumReadingVerdict = EnumReadingVerdict.UNKNOWN
    outcome: EnumReadingOutcome = EnumReadingOutcome.SOURCE_UNREADABLE
    reasons: tuple[str, ...] = ()
    consumers_checked: int = 0
    scans_checked: int = 0
    #: Consumers whose effective value equals the overlay's.
    agreeing: tuple[str, ...] = ()
    mismatches: tuple[ModelConsumerMismatch, ...] = ()
    park_lists: tuple[ModelParkListFinding, ...] = ()
    #: Sources that could not be read, as ``<name>:<cause>``.
    unreadable: tuple[str, ...] = ()
    row_key: str = ""
    ledger: str = "not-written"
    row: str | None = None

    @property
    def failed(self) -> bool:
        """The read itself failed; a NOT_WORKING verdict that was read is not a failure."""
        return (
            self.status == "failed"
            or self.outcome is not EnumReadingOutcome.READ
            or self.ledger.startswith("append-failed")
        )


__all__ = [
    "ModelConsumerMismatch",
    "ModelOverlayJoinCommand",
    "ModelOverlayJoinResult",
    "ModelParkListFinding",
]
