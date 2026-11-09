# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The projection's rules and the goal's scope: overlay data, never constants in the handler.

Every value that decides what counts (which Linear state types are done, which TERMINAL outcomes close a lane as done,
which CLAIM cells attribute a lane to an item, how long a lane with no closing row stays live, how a plan file spells a
task, its ticket and its start criterion) is a field here, read from the node's overlay. The scope is the goal's own
declaration (the goal_scope overlay), read as data: a project, plan, ticket or repository joins the scope by a row in that
declaration and never by a code change.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.models.work_state_projection.enum_work_state_source import (
    EnumWorkStateSource,
)
from omnimarket.models.work_state_projection.model_source_budget import (
    ModelSourceBudget,
)


class ModelPlanParseRules(BaseModel):
    """How a plan file spells a task. Each pattern is a Python regular expression applied per line."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # Names the groups `task` and `title`.
    task_heading_pattern: str
    # Names the group `refs`: a comma separated list of references the task's start criterion requires.
    requires_pattern: str
    # Names the group `ticket`.
    ticket_pattern: str
    # A line inside the task section that marks the task done.
    done_pattern: str


class ModelWorkStateRules(BaseModel):
    """The projection's overlay."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # A ticket id as the tracker spells it (a reference matching it is read as a ticket).
    ticket_id_pattern: str
    # Linear state types that mean the ticket needs no more work.
    ticket_done_state_types: tuple[str, ...] = Field(min_length=1)
    # A ticket description line naming its start criterion; names the group `refs`.
    ticket_requires_pattern: str
    # TERMINAL outcomes that close a lane with nothing left; any other outcome leaves the lane stopped.
    terminal_done_outcomes: tuple[str, ...] = Field(min_length=1)
    # The CLAIM cells that attribute a lane to items, and the item alias prefix each maps to.
    # Example: {"ticket": "ticket", "owns": "pr", "cause": "cause", "key": "key"}.
    attribution_cells: dict[str, str] = Field(min_length=1)
    # Cells of a lane's rows after its CLAIM that say what is left (a pushed branch and head, a pull request).
    left_cells: tuple[str, ...] = ()
    # A lane with no closing row stays live this long after its newest row; past it the lane reads stopped.
    lane_live_hours: float = Field(gt=0)
    # Receipt statuses that mean the lane's engine has ended.
    receipt_ended_statuses: tuple[str, ...] = ()
    plan: ModelPlanParseRules
    # Each source's read budget, timeout, attempts and oldest servable snapshot. Every source must have a row.
    source_budgets: dict[EnumWorkStateSource, ModelSourceBudget]

    @model_validator(mode="after")
    def _every_source_has_a_budget(self) -> ModelWorkStateRules:
        missing = sorted(set(EnumWorkStateSource) - set(self.source_budgets))
        if missing:
            raise ValueError(
                "source_budgets has no row for: " + ", ".join(s.value for s in missing)
            )
        return self


class ModelWorkScope(BaseModel):
    """The goal's scope declaration, as data. An item outside it is projected too, marked paused."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    in_scope_projects: tuple[str, ...] = ()
    in_scope_plans: tuple[str, ...] = ()
    in_scope_tickets: tuple[str, ...] = ()
    in_scope_repos: tuple[str, ...] = ()
    # Pull requests as `repo#n`.
    in_scope_prs: tuple[str, ...] = ()
    paused_prs: tuple[str, ...] = ()
    paused_tickets: tuple[str, ...] = ()


__all__ = ["ModelPlanParseRules", "ModelWorkScope", "ModelWorkStateRules"]
