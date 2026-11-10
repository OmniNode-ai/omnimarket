# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed models of the validator estate read (OMN-23080, piece 4 of the base-first rebuild)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ModelValidatorEstateCommand(_Frozen):
    correlation_id: str = Field(min_length=1)
    write: bool = False
    #: A declared fault to inject on purpose; the row then carries ``drill=yes``.
    fault: Literal["none", "core-unreadable"] = "none"


class ModelFormatClass(_Frozen):
    """One class that subclasses the generic validator base, and what it is made of."""

    repo: str
    module: str
    name: str
    validator_id: str | None
    #: The rule document resolved and holds at least one rule (rules as data).
    rules_as_data: bool
    #: Module-level literal rule tables found in the module (rules hard-coded in code).
    literal_tables: int
    #: The class, its module or its base emits a validator trace on the bus.
    emits_trace: bool

    @property
    def canonical(self) -> bool:
        return self.rules_as_data and self.literal_tables == 0


class ModelHookCount(_Frozen):
    repo: str
    hooks: int
    #: Hooks whose entry runs a validator class already counted as a class (never counted twice).
    covered: int
    pytest: int
    unreadable: bool = False


class ModelPlanTask(_Frozen):
    task: str
    tickets: tuple[str, ...]
    merged_tickets: tuple[str, ...]

    @property
    def state(self) -> Literal["merged", "partial", "open", "no-ticket"]:
        """Merged only when every ticket the plan names for the task has a commit on a declared branch."""
        if not self.tickets:
            return "no-ticket"
        if len(self.merged_tickets) == len(self.tickets):
            return "merged"
        return "partial" if self.merged_tickets else "open"


class ModelValidatorEstateResult(_Frozen):
    correlation_id: str
    status: Literal["completed", "failed"]
    host: str
    outcome: Literal["read", "source-unreadable"]
    unreadable: tuple[str, ...] = ()
    classes: tuple[ModelFormatClass, ...] = ()
    base_emits: bool = False
    bespoke_classes: int = 0
    hooks: tuple[ModelHookCount, ...] = ()
    tasks: tuple[ModelPlanTask, ...] = ()
    #: The head each repository was read at (first 12 characters).
    heads: dict[str, str] = Field(default_factory=dict)
    ledger: str = "not-written"
    row: str = ""
    failure: str | None = None

    @property
    def canonical(self) -> int:
        return sum(1 for c in self.classes if c.canonical)

    @property
    def canonical_emitting(self) -> int:
        return sum(1 for c in self.classes if c.canonical and c.emits_trace)

    @property
    def other_hooks(self) -> int:
        return sum(h.hooks - h.covered for h in self.hooks)

    @property
    def other_classes(self) -> int:
        return sum(1 for c in self.classes if not c.canonical) + self.bespoke_classes

    @property
    def other(self) -> int:
        return self.other_hooks + self.other_classes

    @property
    def estate(self) -> int:
        return self.canonical + self.other

    @property
    def tasks_merged(self) -> int:
        return sum(1 for t in self.tasks if t.state == "merged")
