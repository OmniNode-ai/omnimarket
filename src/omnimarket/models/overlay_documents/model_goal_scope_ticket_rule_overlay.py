# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Model the settings store validates the goal scope ticket rule overlay through.

The document is the one ``omnimarket.handlers.goal_scope_overlay`` reads: the goal's tickets and epics that admit a pull request
(``ticket_roots``) and the bounds of the Linear read that walks a ticket's chain to a root. Every key is required and no
other key is accepted, as the reader requires. A ticket is ``OMN-<n>``. No secret lives here.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr

#: A ticket the rule names as a root.
TicketId = Annotated[StrictStr, Field(pattern=r"^OMN-\d+$")]

#: A whole number of at least 1.
PositiveCount = Annotated[StrictInt, Field(ge=1)]


class ModelGoalScopeTicketRuleOverlay(BaseModel):
    """The ``goal_scope_ticket_rule`` overlay document."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    overlay_version: StrictStr
    ticket: StrictStr
    ruling: StrictStr
    endpoint: Annotated[StrictStr, Field(min_length=1, pattern=r"\S")]
    cache_seconds: PositiveCount
    reads_per_tick: PositiveCount
    read_timeout_seconds: PositiveCount
    max_depth: PositiveCount
    ticket_roots: Annotated[list[TicketId], Field(min_length=1)]


__all__ = ["ModelGoalScopeTicketRuleOverlay", "PositiveCount", "TicketId"]
