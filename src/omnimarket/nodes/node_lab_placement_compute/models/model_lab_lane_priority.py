# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Models of lane priority and plugin parity judgement (OMN-22124).

The rules (``ModelPriorityRules``, ``ModelParityRules``) are read from this node's ``contract.yaml``;
nothing about a repository, a ticket or a marker file is a literal in code.
"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.model_priority_rules import (
    PRIORITY_OTHER as PRIORITY_OTHER,
)
from omnimarket.models.model_priority_rules import (
    ModelPriorityRules as ModelPriorityRules,
)


class ModelLanePriorityOverlay(BaseModel):
    """Keys ``lane_priority.*`` of the private overlay: factory machinery, defined once."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Tickets of the factory epics. A lane whose ticket's parent is listed takes the contract's ``factory_class``.
    factory_epics: tuple[str, ...] = Field(..., min_length=1)


class ModelParityRules(BaseModel):
    """The ``parity`` block of the node contract."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ignored_top_level: tuple[str, ...] = ()


class ModelLanePriorityRequest(BaseModel):
    """One lane and, when the pool reading is known, what the pool has free."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str | None = Field(None, description="owner/name or the bare repository name")
    ticket: str | None = None
    parent: str | None = None
    #: The ticket text the caller fetched, read for its ``Class:`` line; None: the caller read no ticket.
    ticket_text: str | None = None
    #: The factory epics of the private overlay (``lane_priority.factory_epics``); a ticket whose parent is one is factory work.
    factory_epics: tuple[str, ...] = ()
    #: The class the caller declared (``--priority-class`` or the lab-fill environment); a name that is not a class is no declaration.
    declared: str | None = None
    #: Repositories of a class that the private overlay lists (class -> names); added to the contract's.
    private_repos: Mapping[str, tuple[str, ...]] = Field(default_factory=dict)
    #: Free slots across the pool; None: the pool is not judged, only the class.
    free_slots: int | None = Field(None, ge=0)
    #: Classes with a unit waiting for a slot.
    waiting: tuple[str, ...] = ()


class ModelLanePriorityResult(BaseModel):
    """The lane's class, where it came from, and whether the pool admits it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    priority_class: str
    #: ``declared``, ``class-line``, ``ticket``, ``factory-epic``, ``repository`` or ``default``.
    source: str
    #: ``PRIORITY-HOLDBACK: ...`` or ``NO-FREE-SLOT: ...`` when the pool refuses the lane, else None.
    refusal: str | None = None
    #: ``CLASS-LINE-MISSING: ...`` or ``CLASS-LINE-UNKNOWN: ...`` when the ticket's text gave no class and the lane fell back.
    fallback: str | None = None

    @property
    def admitted(self) -> bool:
        return self.refusal is None


class ModelPluginParityRequest(BaseModel):
    """An installed plugin tree and its shipped revision's, each ``path -> blob id``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    installed: Mapping[str, str]
    shipped: Mapping[str, str]


class ModelPluginParityResult(BaseModel):
    """``ok`` when the trees agree outside the runtime markers, else ``modified`` with the paths."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    state: str
    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    changed: tuple[str, ...] = ()
    #: Runtime markers present in the installed tree and left out of the comparison.
    ignored: tuple[str, ...] = ()

    @property
    def detail(self) -> str:
        parts = [
            f"{label} {', '.join(paths)}"
            for label, paths in (
                ("added", self.added),
                ("removed", self.removed),
                ("changed", self.changed),
            )
            if paths
        ]
        return "; ".join(parts) or "identical"
