# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The projection: every work item with its state, as of the request's clock reading."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from omnimarket.events.work_state_projection.enum_scope_status import (
    EnumScopeStatus,
)
from omnimarket.events.work_state_projection.enum_work_item_state import (
    EnumWorkItemState,
)
from omnimarket.events.work_state_projection.model_work_item import (
    ModelWorkItem,
)


class ModelWorkStateResult(BaseModel):
    """The items in key order, the sources that could not be read and a count per state."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    as_of: datetime
    items: tuple[ModelWorkItem, ...]
    unread: tuple[str, ...] = ()
    counts: dict[str, int]

    def ready(self) -> tuple[ModelWorkItem, ...]:
        """The items a dispatcher may start a lane on: in scope and available, or in scope and stopped."""
        return tuple(
            i
            for i in self.items
            if i.scope is EnumScopeStatus.IN_SCOPE
            and i.state in (EnumWorkItemState.AVAILABLE, EnumWorkItemState.STOPPED)
        )


__all__ = ["ModelWorkStateResult"]
