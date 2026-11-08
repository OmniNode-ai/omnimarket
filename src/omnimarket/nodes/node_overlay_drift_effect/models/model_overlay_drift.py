# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed models of the overlay drift read: the home's pins, each overlay's verdict, the run."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.model_reading import EnumReadingOutcome, EnumReadingVerdict


class EnumOverlayVerdict(StrEnum):
    """One overlay against the runtime's model."""

    ACCEPTED = "accepted"
    #: The installed model refused the document (the rung_order case).
    REJECTED = "rejected"
    #: The pin names a module or class the runtime does not have.
    MODEL_MISSING = "model_missing"
    #: The overlay file is not YAML.
    UNREADABLE = "unreadable"
    #: A pin names an overlay the home does not hold.
    OVERLAY_MISSING = "overlay_missing"


class ModelOverlayPin(BaseModel):
    """One line of the home's pins file: ``<overlay id> <omnimarket revision> <module>:<class>``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    overlay_id: str = Field(pattern=r"^[a-z0-9_]+$")
    revision: str = Field(min_length=1)
    model: str = Field(pattern=r"^[A-Za-z_][\w.]*:[A-Za-z_]\w*$")


class ModelOverlayCheck(BaseModel):
    """One overlay's verdict; ``fields`` names refused fields, never a value."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    overlay_id: str
    verdict: EnumOverlayVerdict
    fields: tuple[str, ...] = ()
    error_class: str = ""


class ModelRuntimeIdentity(BaseModel):
    """What the runtime interpreter has installed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str
    revision: str


class ModelRuntimeValidation(BaseModel):
    """The runtime's answer for every pinned overlay it was given."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    runtime: ModelRuntimeIdentity
    results: tuple[ModelOverlayCheck, ...]


class ModelOverlayDriftCommand(BaseModel):
    """Run the drift read once; ``write`` false reads and appends nothing."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(min_length=1)
    write: bool = True
    #: Set when the piece upstream of this read failed under this correlation id: the read does not start,
    #: reads nothing and answers with a typed failure that carries this code (the join's error shape).
    upstream_failure: str | None = Field(default=None, min_length=1)
    #: Set when the piece upstream succeeded and this read takes that success as its input: the correlation id of the upstream
    #: chain. The row this run writes names it in the overlay's upstream cell, so the floor reads the row as this piece's
    #: answer to that chain.
    upstream_cid: str | None = Field(default=None, min_length=1)


class ModelOverlayDriftCounts(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    checked: int = 0
    accepted: int = 0
    rejected: int = 0
    model_missing: int = 0
    unreadable: int = 0
    overlay_missing: int = 0
    unpinned: int = 0
    #: Pins whose omnimarket revision is not on the merged branch (a squashed branch commit).
    pin_unmerged: int = 0

    @property
    def failing(self) -> int:
        return (
            self.rejected + self.model_missing + self.unreadable + self.overlay_missing
        )

    @property
    def drift(self) -> int:
        """A model the runtime lacks is the runtime lagging the models, not drift."""
        return self.rejected + self.unreadable + self.overlay_missing


class ModelOverlayDriftResult(BaseModel):
    """The run: its verdict, counts, each failing overlay, and what happened on the ledger."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    status: Literal["completed", "failed"]
    host: str
    failure: str | None = None
    verdict: EnumReadingVerdict = EnumReadingVerdict.UNKNOWN
    outcome: EnumReadingOutcome = EnumReadingOutcome.SOURCE_UNREADABLE
    reasons: tuple[str, ...] = ()
    counts: ModelOverlayDriftCounts = Field(default_factory=ModelOverlayDriftCounts)
    failing: tuple[ModelOverlayCheck, ...] = ()
    unpinned: tuple[str, ...] = ()
    #: Overlays whose pin names a revision the merged branch does not hold.
    unmerged_pins: tuple[str, ...] = ()
    runtime: ModelRuntimeIdentity | None = None
    home_revision: str = "unknown"
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
    "EnumOverlayVerdict",
    "ModelOverlayCheck",
    "ModelOverlayDriftCommand",
    "ModelOverlayDriftCounts",
    "ModelOverlayDriftResult",
    "ModelOverlayPin",
    "ModelRuntimeIdentity",
    "ModelRuntimeValidation",
]
