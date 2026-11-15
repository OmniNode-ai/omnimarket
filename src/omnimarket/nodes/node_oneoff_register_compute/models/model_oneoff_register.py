# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed models of the one-off register (OMN-19998).

The register of one-offs: every automatic process that computes data outside the
runtime, with what it is, where it runs, its replacement node or plan, its owner
and its state. No register and no liveness facts live here: the register policy
arrives from the ``node_oneoff_register_compute`` overlay and the liveness rows from the
``automation_liveness`` overlay.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class EnumOneOffCategory(StrEnum):
    """What the one-off is: a container, a unit, a tree or venv, a hand config or a hand script."""

    C1 = "C1"
    C2 = "C2"
    C3 = "C3"
    C4 = "C4"
    C5 = "C5"


class EnumReplacementKind(StrEnum):
    """What stands in for the one-off when it goes: a node, a plan or nothing."""

    NODE = "node"
    PLAN = "plan"
    NONE = "none"


class EnumOneOffState(StrEnum):
    """How far the one-off's replacement has come."""

    NO_REPLACEMENT = "no_replacement"
    REPLACEMENT_PLANNED = "replacement_planned"
    REPLACEMENT_BUILT = "replacement_built"
    REPLACEMENT_LIVE = "replacement_live"
    RETIRED = "retired"


class EnumBypassReading(StrEnum):
    """What the one-off's bypass was read as: yes, no or not read yet."""

    YES = "yes"
    NO = "no"
    UNREAD = "unread"


class ModelOneOffEntry(BaseModel):
    """One register row: what it is, where it runs, its replacement, its owner and its state."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    one_off_id: str = Field(min_length=1)
    what: str = Field(min_length=1)
    where: str = Field(min_length=1)
    category: EnumOneOffCategory
    owner: str = Field(min_length=1)
    owner_repo: str = Field(min_length=1)
    state: EnumOneOffState
    replacement_kind: EnumReplacementKind
    replacement_ref: str = ""
    bypass: EnumBypassReading
    liveness_process_id: str = ""
    basis: str = Field(min_length=1)

    @field_validator("bypass", mode="before")
    @classmethod
    def _bare_yes_no_is_a_reading(cls, value: Any) -> Any:
        """An overlay author's unquoted ``no`` or ``yes`` reads as a YAML boolean; it is the reading, not a refusal."""
        if isinstance(value, bool):
            return EnumBypassReading.YES if value else EnumBypassReading.NO
        return value

    @model_validator(mode="after")
    def _state_matches_replacement(self) -> ModelOneOffEntry:
        one_off = self.one_off_id
        kind = self.replacement_kind
        state = self.state
        has_ref = kind in (EnumReplacementKind.NODE, EnumReplacementKind.PLAN)
        if has_ref and not self.replacement_ref:
            raise ValueError(
                f"one-off {one_off!r}: a {kind.value} replacement names no ref"
            )
        if kind is EnumReplacementKind.NONE and self.replacement_ref:
            raise ValueError(f"one-off {one_off!r}: no replacement names a ref")
        if kind is EnumReplacementKind.NONE and state not in (
            EnumOneOffState.NO_REPLACEMENT,
            EnumOneOffState.RETIRED,
        ):
            raise ValueError(
                f"one-off {one_off!r}: no replacement with state {state.value}"
            )
        if (
            state is EnumOneOffState.NO_REPLACEMENT
            and kind is not EnumReplacementKind.NONE
        ):
            raise ValueError(
                f"one-off {one_off!r}: no_replacement with a {kind.value} replacement"
            )
        if state is EnumOneOffState.REPLACEMENT_PLANNED and not has_ref:
            raise ValueError(
                f"one-off {one_off!r}: replacement_planned without a plan or node"
            )
        if (
            state
            in (EnumOneOffState.REPLACEMENT_BUILT, EnumOneOffState.REPLACEMENT_LIVE)
            and kind is not EnumReplacementKind.NODE
        ):
            raise ValueError(f"one-off {one_off!r}: {state.value} without a node")
        return self


class ModelOneOffExclusion(BaseModel):
    """A liveness process the register deliberately leaves out."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    process_id: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class ModelOneOffRegisterPolicy(BaseModel):
    """The ``node_oneoff_register_compute`` overlay: the entries, the exclusions and the scope, as deployment data."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    scope_hosts: tuple[str, ...] = Field(min_length=1)
    scope_states: tuple[str, ...] = Field(min_length=1)
    entries: tuple[ModelOneOffEntry, ...]
    exclusions: tuple[ModelOneOffExclusion, ...] = ()

    @model_validator(mode="after")
    def _ids_are_unique(self) -> ModelOneOffRegisterPolicy:
        ids = [entry.one_off_id for entry in self.entries]
        if len(ids) != len(set(ids)):
            raise ValueError("the register lists a one-off more than once")
        process_ids = [
            entry.liveness_process_id
            for entry in self.entries
            if entry.liveness_process_id
        ]
        if len(process_ids) != len(set(process_ids)):
            raise ValueError(
                "the register names a liveness process for more than one one-off"
            )
        exclusion_ids = [exclusion.process_id for exclusion in self.exclusions]
        if len(exclusion_ids) != len(set(exclusion_ids)):
            raise ValueError("the register excludes a process more than once")
        overlap = set(exclusion_ids) & set(process_ids)
        if overlap:
            raise ValueError(
                f"processes {sorted(overlap)} are both excluded and registered"
            )
        return self


class ModelLivenessProcessRef(BaseModel):
    """One liveness row: which process, on which host, in which state."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    process_id: str
    host: str
    state: str


class ModelLivenessOverlayRefs(BaseModel):
    """The ``automation_liveness`` overlay as this node reads it: the process rows."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    processes: tuple[ModelLivenessProcessRef, ...]


class ModelOneOffRegisterRequest(BaseModel):
    """Everything one register reading needs; the node reads no store and no file."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    policy: ModelOneOffRegisterPolicy
    liveness_processes: tuple[ModelLivenessProcessRef, ...]
    node_names: tuple[str, ...]


class ModelOneOffReading(BaseModel):
    """One one-off's reading: its state, its replacement and what the register found on it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    one_off_id: str
    state: EnumOneOffState
    replacement_kind: EnumReplacementKind
    replacement_ref: str = ""
    replacement_found: bool | None = None
    findings: tuple[str, ...] = ()


class ModelOneOffRegisterResult(BaseModel):
    """The register reading: every one-off, every way it failed and the counts it kept."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    passed: bool
    readings: tuple[ModelOneOffReading, ...]
    unregistered_processes: tuple[str, ...] = ()
    unknown_processes: tuple[str, ...] = ()
    missing_replacement_nodes: tuple[str, ...] = ()
    unplanned: tuple[str, ...] = ()
    bypass_yes: tuple[str, ...] = ()
    bypass_unread: tuple[str, ...] = ()
    state_counts: tuple[tuple[str, int], ...] = ()
    one_off_count: int
    exclusion_count: int
