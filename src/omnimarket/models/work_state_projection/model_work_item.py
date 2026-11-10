# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One projected work item."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.models.work_state_projection.enum_criterion_reading import (
    EnumCriterionReading,
)
from omnimarket.models.work_state_projection.enum_scope_status import (
    EnumScopeStatus,
)
from omnimarket.models.work_state_projection.enum_work_item_kind import (
    EnumWorkItemKind,
)
from omnimarket.models.work_state_projection.enum_work_item_state import (
    EnumWorkItemState,
)


class ModelCriterionPart(BaseModel):
    """One reference of a start criterion and how it reads."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ref: str
    reading: EnumCriterionReading
    detail: str = ""


class ModelWorkItem(BaseModel):
    """A work item with its state, the reason, its start criterion's reading and the lanes attributed to it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str
    kind: EnumWorkItemKind
    title: str = ""
    ticket: str | None = None
    state: EnumWorkItemState
    reason: str
    scope: EnumScopeStatus
    criterion: EnumCriterionReading
    criterion_parts: tuple[ModelCriterionPart, ...] = ()
    # The items whose state holds this one back (blocked), by key.
    blocking: tuple[str, ...] = ()
    # Every lane attributed to this item through its CLAIM cells, oldest first.
    lanes: tuple[str, ...] = ()
    # The live lane working it (in progress).
    live_lane: str | None = None
    # What a stopped lane left (its pushed branch and head, its outcome).
    left: str | None = None
    # A lane item only: every alias its CLAIM cells name (`ticket:OMN-1`, `cause:stoppage:x`, `pr:repo#1`, a key), so a
    # reader that draws its own rows (the floor) attributes the lane to a row from the CLAIM and not from a typed owner.
    attributes: tuple[str, ...] = ()
    # Who owes the item, where the source names one (a commitment's owner).
    owner: str | None = None
    # A ticket item only (OMN-23313): the tracker assignee and parent ticket, so a reader that judges ownership by epic
    # (the floor's delegation:idle row) reads them from the projection and not from a second tracker read.
    assignee: str | None = None
    parent: str | None = None


__all__ = ["ModelCriterionPart", "ModelWorkItem"]
