# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed models of the broker authentication refusal reading: the counted window and the run."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.model_reading import EnumReadingOutcome, EnumReadingVerdict


class ModelRefusalMinute(BaseModel):
    """Refusals the broker logged in one UTC minute (``YYYY-MM-DDTHH:MM``)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    minute: str = Field(min_length=16, max_length=16)
    count: int = Field(ge=1)


class ModelRefusalWindow(BaseModel):
    """What the broker's log says over the window, counts and one sample line only."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    total: int = Field(ge=0)
    minutes: tuple[ModelRefusalMinute, ...] = ()
    #: UTC ``YYYY-MM-DDTHH:MM:SS`` of the first and last refusal; empty when there is none.
    first_seen: str = ""
    last_seen: str = ""
    #: The first refusal line, cut to the overlay's length, without its timestamp.
    sample: str = ""


class ModelBrokerAuthRefusalCommand(BaseModel):
    """Run the reading once; ``write`` false reads and appends nothing."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(min_length=1)
    write: bool = True


class ModelBrokerAuthRefusalResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    status: Literal["completed", "failed"]
    host: str
    failure: str | None = None
    verdict: EnumReadingVerdict = EnumReadingVerdict.UNKNOWN
    outcome: EnumReadingOutcome = EnumReadingOutcome.SOURCE_UNREADABLE
    reasons: tuple[str, ...] = ()
    window: ModelRefusalWindow | None = None
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
    "ModelBrokerAuthRefusalCommand",
    "ModelBrokerAuthRefusalResult",
    "ModelRefusalMinute",
    "ModelRefusalWindow",
]
