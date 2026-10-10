# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""One read of a live ticket's description, readable or not (OMN-20858)."""

from __future__ import annotations

from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ModelTicketCriteriaRead(BaseModel):
    """The live ticket as one read returned it.

    Exactly one of ``description`` and ``unavailable_reason`` is set, so an
    unreadable ticket can never be taken for a ticket with no criteria.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticket_id: str = Field(..., min_length=1)
    description: str | None = Field(
        default=None,
        description="The ticket body as read. Empty string is a readable empty body.",
    )
    unavailable_reason: str | None = Field(
        default=None,
        description="Why the ticket could not be read. Never carries a credential.",
    )

    @model_validator(mode="after")
    def _exactly_one(self) -> Self:
        if (self.description is None) == (self.unavailable_reason is None):
            raise ValueError(
                "exactly one of description and unavailable_reason is set; got "
                f"description={self.description!r}, "
                f"unavailable_reason={self.unavailable_reason!r} for {self.ticket_id}"
            )
        return self


__all__ = ["ModelTicketCriteriaRead"]
