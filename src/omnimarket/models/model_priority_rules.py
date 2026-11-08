# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Lane priority rules shared by placement and landing decisions.

The lab placement contract declares the classes, holdbacks and classification rules.
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
