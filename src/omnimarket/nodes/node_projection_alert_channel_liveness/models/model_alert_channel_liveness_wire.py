# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The liveness result as it arrives off the checked-event topic.

WHY A LOCAL WIRE MODEL
    The producer owns strict models for constructing its results. A reader
    must tolerate fields a later producer adds, including inside the verdict,
    without turning a valid measurement into a dead-letter entry. These local
    models deliberately ignore extras and import no private producer package.

WHAT A THROTTLED TICK MEANS
    A result with probed=false carries no new measurement. Its absent verdict
    is valid, and the fold skips it. A probed result without a verdict is
    malformed instead: inventing a status would record a measurement that
    nobody made.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ModelAlertChannelVerdictWire(BaseModel):
    """One measured verdict, with the producer's four status tokens."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    status: Literal["LIVE", "DEAD", "NOT_CONFIGURED", "PROBE_ERROR"] = Field(
        ..., description="Measured channel state; only LIVE proves health."
    )
    reason: str = Field(
        ..., min_length=1, description="The probe's explanation, retained verbatim."
    )
    slack_error: str | None = Field(
        default=None, description="Slack's error token, when one was returned."
    )


class ModelAlertChannelLivenessResultWire(BaseModel):
    """One result delivered by the alert-channel liveness checker."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    probed: bool = Field(
        ..., description="False for a throttled tick, which is not a verdict."
    )
    verdict: ModelAlertChannelVerdictWire | None = Field(
        default=None, description="The measured verdict, required when probed is true."
    )
    probe_interval_seconds: int = Field(
        ..., gt=0, description="The interval the producer actually used."
    )
    failure_surfaced: bool = Field(
        ..., description="Whether the producer surfaced this failure independently."
    )
    checked_at: datetime | None = Field(
        default=None,
        description=(
            "Event time when carried on the payload. The current producer has "
            "none; the writer can supply the envelope time instead."
        ),
    )

    @model_validator(mode="after")
    def require_probed_verdict(self) -> Self:
        """A completed probe must describe what it measured."""
        if self.probed and self.verdict is None:
            raise ValueError("a probed liveness result must carry a verdict")
        return self


__all__ = ["ModelAlertChannelLivenessResultWire", "ModelAlertChannelVerdictWire"]
