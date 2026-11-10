# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed request, settings and result of the walk's lifecycle-controls grading."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.models.model_reading import EnumReadingVerdict


class _FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ModelLifecycleControlsSettings(_FrozenModel):
    """The deployment facts of the controls, supplied by an overlay and never by a default."""

    # The destination the walk blocked; a recorded connect to it names the rejected connect.
    blocked_destination: str = Field(min_length=1)
    # The name the graded tree looks up twice; the second answer comes from the cache.
    lookup_name: str = Field(min_length=1)
    # The recorder call name the forked child makes towards the blocked destination.
    forked_child_call: str = Field(min_length=1)


class ModelTreeInput(_FrozenModel):
    """One process tree: its root identity and the recorder's lines for it."""

    name: str = Field(min_length=1)
    role: Literal["graded", "control"]
    # A graded tree is expected to read working; a control tree is built to read blind.
    expect: Literal["working", "blind-image"]
    root_pid: int = Field(ge=1)
    root_pidversion: int = Field(ge=0)
    recorder_jsonl: str

    @model_validator(mode="after")
    def _role_matches_expectation(self) -> ModelTreeInput:
        expected = "working" if self.role == "graded" else "blind-image"
        if self.expect != expected:
            raise ValueError(f"a {self.role} tree expects {expected}")
        return self


class ModelCaptureEvidence(_FrozenModel):
    """Counts the caller took from the packet capture."""

    packets_to_blocked_destination: int = Field(ge=0)
    dns_queries_from_graded_tree: int = Field(ge=0)


class ModelLifecycleControlsRequest(_FrozenModel):
    """Everything the grading is a function of; it reads no file, clock, network or environment."""

    trees: tuple[ModelTreeInput, ...] = Field(min_length=1)
    observer_jsonl: str
    observer_timebase_numer: int = Field(ge=1)
    observer_timebase_denom: int = Field(ge=1)
    capture: ModelCaptureEvidence
    settings: ModelLifecycleControlsSettings

    @model_validator(mode="after")
    def _trees_are_distinct_and_graded(self) -> ModelLifecycleControlsRequest:
        names = [tree.name for tree in self.trees]
        if len(names) != len(set(names)):
            raise ValueError("tree names repeat")
        if not any(tree.role == "graded" for tree in self.trees):
            raise ValueError("at least one tree is graded")
        return self


class ModelControlOutcome(_FrozenModel):
    name: str
    passed: bool


class ModelTreeResult(_FrozenModel):
    name: str
    role: Literal["graded", "control"]
    verdict: EnumReadingVerdict
    # Token-safe reason codes, sorted and unique; empty only for WORKING.
    reasons: tuple[str, ...]
    exec_obligations_total: int
    exec_obligations_met: int
    fork_events: int
    # A graded tree carries the three graded controls; a control tree the one control it is.
    controls: tuple[ModelControlOutcome, ...]


class ModelLifecycleControlsResult(_FrozenModel):
    lifecycle_controls: Literal["passed", "failed"]
    # Token-safe reason codes, sorted and unique; empty only when passed.
    failed_reasons: tuple[str, ...]
    observer_events: int
    trees: tuple[ModelTreeResult, ...]
