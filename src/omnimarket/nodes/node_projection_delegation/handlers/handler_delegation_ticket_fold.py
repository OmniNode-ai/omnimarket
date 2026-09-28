# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure fold that reads the ticket id off a delegate-skill terminal.

Returns the delegation_events column for it. The effect writers persist
what it returns and decide nothing. A terminal with no ticket yields no
column, so a ticketless re-emit leaves a stored ticket alone. A malformed
ticket is refused by name and yields no column, so it never dead-letters
the delegation row and is never guessed into a ticket.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.models.delegation.delegation_ticket_id import ticket_id_refusal
from omnimarket.models.delegation.wire.model_delegate_skill_terminal_projection import (
    ModelDelegateSkillTerminalProjection,
)


class ModelDelegationTicketFold(BaseModel):
    """Represents the outcome of folding a delegation ticket."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticket_id: str | None = Field(default=None)
    ticket_id_refusal: str | None = Field(default=None)

    @model_validator(mode="after")
    def _at_most_one_outcome(self) -> ModelDelegationTicketFold:
        """Ensure that at most one of ticket_id and ticket_id_refusal is set."""
        if self.ticket_id is not None and self.ticket_id_refusal is not None:
            raise ValueError(
                "a ticket fold holds exactly one of ticket_id and ticket_id_refusal"
            )
        return self

    def row_columns(self) -> dict[str, object]:
        """Return the row columns for the delegation_events table."""
        if self.ticket_id is not None:
            return {"ticket_id": self.ticket_id}
        return {}


class HandlerDelegationTicketFold:
    """Handles the delegation ticket fold operation."""

    def handle(
        self, request: ModelDelegateSkillTerminalProjection
    ) -> ModelDelegationTicketFold:
        """Process the request and return the appropriate fold model."""
        value = request.ticket_id
        if value is None:
            return ModelDelegationTicketFold()
        refusal = ticket_id_refusal(value)
        if refusal is not None or not isinstance(value, str):
            return ModelDelegationTicketFold(ticket_id_refusal=refusal)
        return ModelDelegationTicketFold(ticket_id=value)


__all__ = ["HandlerDelegationTicketFold", "ModelDelegationTicketFold"]
