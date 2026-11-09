# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The delegation:idle row (OMN-23313): an operator-owned delegation ticket that can start but has no running lane.

Which tickets count is overlay data, never a constant: the delegation epics (a ticket belongs to delegation when its
parent chain reaches one) and the tracker assignees the operator owns work as. The ticket's start criterion and its
assignee are read from the work-state projection; the row's colour is judged by the floor with the same ``live_lanes``
rule every other row uses.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ModelDelegationIdleOverlay(_Frozen):
    """Which tickets the delegation:idle row looks at, and how it is worded."""

    # Delegation epics: a ticket is delegation work when its parent chain reaches one of these (or it is a child of one).
    epics: tuple[str, ...] = Field(min_length=1)
    # The tracker assignee values (id or name, as the Linear snapshot spells them) that mean the operator owns the ticket.
    operator_assignees: tuple[str, ...] = Field(min_length=1)
    title: str = Field(min_length=1)


class ModelDelegationTicketIn(_Frozen):
    """One operator-owned delegation ticket whose start criterion reads true, as the sources read it."""

    ticket: str = Field(min_length=1)
    title: str = ""
    # The tracker assignee that matched the overlay; lanes are not read here, the floor judges them.
    assignee: str = ""


__all__ = ["ModelDelegationIdleOverlay", "ModelDelegationTicketIn"]
