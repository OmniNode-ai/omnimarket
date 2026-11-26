# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Lane priority rules shared by placement and landing decisions.

The lab placement contract declares the classes, holdbacks and classification rules.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator

#: The class a unit of no named class takes, and the last of the contract's classes.
PRIORITY_OTHER = "other"


class ModelRedCheckRules(BaseModel):
    """The ``priority.red_checks`` block (OMN-26624): which delegation checks count, the class a red one takes, where read."""

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    #: The class a lane takes when its ticket text or brief names a red declared check.
    class_: str = Field(..., alias="class")
    #: The delegation check ids that count; a red check outside the list changes no lane's class.
    checks: tuple[str, ...] = Field(..., min_length=1)
    #: Where the result is read, in order: the first source that reads decides, a later one is read only when it fails.
    sources: tuple[str, ...] = Field(..., min_length=1)

    @model_validator(mode="after")
    def _consistent(self) -> ModelRedCheckRules:
        if len({c.upper() for c in self.checks}) != len(self.checks):
            raise ValueError("priority.red_checks.checks repeats a check")
        if len(set(self.sources)) != len(self.sources):
            raise ValueError("priority.red_checks.sources repeats a source")
        return self

    def declared(self, check: str) -> str | None:
        """The declared spelling of ``check`` (case-insensitive), else None."""
        wanted = check.strip().upper()
        return next((c for c in self.checks if c.upper() == wanted), None)

    def named_in(self, *texts: str | None) -> tuple[str, ...]:
        """The declared checks the texts name as whole words (``C13`` but not ``C130`` or ``abC13``), in declared order."""
        text = "\n".join(t for t in texts if t)
        return tuple(
            c
            for c in self.checks
            if re.search(rf"(?<![A-Za-z0-9]){re.escape(c)}(?![A-Za-z0-9])", text, re.I)
        )


class ModelRedCheckReading(BaseModel):
    """What a caller read of the delegation check results: the red check ids, or why they could not be read.

    ``unreadable`` set means the result is not known: it is never read as green."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    red: tuple[str, ...] = ()
    #: The source the result came from (one of the contract's ``sources``); empty when unreadable.
    read_from: str = ""
    #: Why no source could be read, one ``<source>: <reason>`` per source; None when a source was read.
    unreadable: str | None = None


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
    #: OMN-26624: the delegation check input; None declares none, and no lane takes a red-check class.
    red_checks: ModelRedCheckRules | None = None

    @model_validator(mode="after")
    def _consistent(self) -> ModelPriorityRules:
        if self.red_checks is not None and self.red_checks.class_ not in self.classes:
            raise ValueError(
                f"priority.red_checks.class {self.red_checks.class_!r} is not a class of {list(self.classes)}"
            )
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
