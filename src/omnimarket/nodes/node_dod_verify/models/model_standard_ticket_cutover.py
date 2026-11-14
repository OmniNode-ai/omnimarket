# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The standard-ticket cutover dod-verify judges a ticket against (OMN-23771)."""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, ConfigDict, Field


class ModelStandardTicketCutover(BaseModel):
    """The overlay's cutover: a date, and the first ticket number Linear assigned after it.

    Hosted CI cannot read a ticket's creation date, so it compares ticket numbers;
    the date is carried for the recorded ``grandfathered`` verdict.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    cutover_date: date = Field(..., description="The cutover date.")
    first_ticket_number: int = Field(
        ...,
        ge=1,
        description="The first ticket number Linear assigned on or after the cutover.",
    )


__all__: list[str] = ["ModelStandardTicketCutover"]
