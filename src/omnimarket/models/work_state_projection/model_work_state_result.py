# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The projection: every work item with its state, as of the request's clock reading."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from omnimarket.models.work_state_projection.enum_criterion_reading import (
    EnumCriterionReading,
)
from omnimarket.models.work_state_projection.enum_scope_status import (
    EnumScopeStatus,
)
from omnimarket.models.work_state_projection.enum_work_item_state import (
    EnumWorkItemState,
)
from omnimarket.models.work_state_projection.model_source_budget import (
    ModelSourceStatus,
)
from omnimarket.models.work_state_projection.model_work_item import (
    ModelWorkItem,
)

# The prefixes of the `unread` entries that name a source of lane or pull request ownership (the runtime writes them).
# While one of these cannot be read, an item may already be owned, so nothing is offered as work.
OWNERSHIP_UNREAD_PREFIXES = ("ledger:", "pr-watcher:")


class ModelWorkStateResult(BaseModel):
    """The items in key order, the sources that could not be read and a count per state."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    as_of: datetime
    items: tuple[ModelWorkItem, ...]
    unread: tuple[str, ...] = ()
    source_status: tuple[ModelSourceStatus, ...] = ()
    counts: dict[str, int]

    def ready(self) -> tuple[ModelWorkItem, ...]:
        """The items a dispatcher may start a lane on: in scope and available, or in scope and stopped.

        A stopped item still needs its start criterion to read true, and nothing is ready while a source of ownership
        (the lane rows, the pull request watcher) was unread: its absence is not proof that no lane or pull request owns
        the item.
        """
        if any(u.startswith(OWNERSHIP_UNREAD_PREFIXES) for u in self.unread):
            return ()
        return tuple(
            i
            for i in self.items
            if i.scope is EnumScopeStatus.IN_SCOPE
            and (
                i.state is EnumWorkItemState.AVAILABLE
                or (
                    i.state is EnumWorkItemState.STOPPED
                    and i.criterion is EnumCriterionReading.TRUE
                )
            )
        )


__all__ = ["ModelWorkStateResult"]
