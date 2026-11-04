# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Models of lane priority and plugin parity judgement (OMN-22124).

The rules (``ModelPriorityRules``, ``ModelParityRules``) are read from this node's ``contract.yaml``;
nothing about a repository, a ticket or a marker file is a literal in code.
"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator

#: The class a unit of no named class takes, and the last of the contract's classes.
PRIORITY_OTHER = "other"


class ModelPriorityRules(BaseModel):
    """The ``priority`` block of the node contract."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    classes: tuple[str, ...] = Field(..., min_length=1)
    #: Per class, the most free pool slots a unit of the class leaves to the classes above it that have a unit waiting.
    holdback: dict[str, int]
    #: The class a lane of factory machinery takes (a lane whose ticket's parent is a listed factory epic).
    factory_class: str
    repos: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    tickets: dict[str, tuple[str, ...]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _consistent(self) -> ModelPriorityRules:
        if len(set(self.classes)) != len(self.classes):
            raise ValueError("priority.classes repeats a class")
        if self.classes[-1] != PRIORITY_OTHER:
            raise ValueError(f"priority.classes must end with {PRIORITY_OTHER}")
        if set(self.holdback) != set(self.classes) or any(
            n < 0 for n in self.holdback.values()
        ):
            raise ValueError(
                f"priority.holdback must give each of {list(self.classes)} a count of 0 or more"
            )
        if self.factory_class not in self.classes:
            raise ValueError(
                f"priority.factory_class {self.factory_class!r} is not a class of {list(self.classes)}"
            )
        for section, mapping in (("repos", self.repos), ("tickets", self.tickets)):
            unknown = sorted(set(mapping) - set(self.classes))
            if unknown:
                raise ValueError(
                    f"priority.{section} names {unknown}, not classes of {list(self.classes)}"
                )
        return self

    def with_repos(self, extra: Mapping[str, tuple[str, ...]]) -> ModelPriorityRules:
        """These rules with ``extra`` (class -> repositories, from the private overlay) added; a class that is not one of the contract's is refused."""
        merged = {cls: tuple(names) for cls, names in self.repos.items()}
        for cls, names in extra.items():
            merged[cls] = (*merged.get(cls, ()), *names)
        return ModelPriorityRules.model_validate({**self.model_dump(), "repos": merged})

    def class_of_repo(self, repo: str | None) -> str:
        """The class the contract lists ``repo`` (``owner/name``, ``name`` or ``name#n``) under, else ``other``."""
        name = str(repo or "").split("#", 1)[0].rsplit("/", 1)[-1].strip().lower()
        if not name:
            return PRIORITY_OTHER
        for cls in self.classes:
            if name in {r.lower() for r in self.repos.get(cls, ())}:
                return cls
        return PRIORITY_OTHER

    def class_of_ticket(self, ticket: str | None, parent: str | None = None) -> str:
        """The class the contract lists ``ticket``, else ``parent``, under, else ``other``."""
        for candidate in (ticket, parent):
            key = str(candidate or "").strip().upper()
            if not key:
                continue
            for cls in self.classes:
                if key in {t.upper() for t in self.tickets.get(cls, ())}:
                    return cls
        return PRIORITY_OTHER


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
