# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed models of join J1, the code graph to hosts and capacity (rebuild plan 4.1, OMN-22904).

The code graph (piece 1) gives each node a used-or-unused verdict. Host discovery and placement (piece 2) start only
on a graph run that carries a verdict for every node they depend on. A verdict that is missing is a typed failure, and
the host stage does not start.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class EnumGraphNodeVerdict(StrEnum):
    """What the graph says of one node: a root reaches it, no root reaches it, or the graph has no such node."""

    USED = "used"
    UNUSED = "unused"
    MISSING = "missing"


class ModelGraphNodeVerdict(BaseModel):
    """The graph's verdict for one node and how many of its non-test modules the graph holds."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    node: str = Field(min_length=1)
    verdict: EnumGraphNodeVerdict
    modules: int = Field(ge=0)
    reached: int = Field(ge=0)


class ModelGraphRunVerdicts(BaseModel):
    """One graph run as the join reads it: its correlation id, whether it completed, and its verdicts by node."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(min_length=1)
    status: Literal["completed", "failed"]
    verdicts: tuple[ModelGraphNodeVerdict, ...] = ()
    #: Why the run failed; empty when it completed.
    failure: str = ""

    @model_validator(mode="after")
    def _shape(self) -> ModelGraphRunVerdicts:
        if (self.status == "failed") != bool(self.failure):
            raise ValueError(
                "a failed run carries its failure and a completed one does not"
            )
        return self


class ModelJoinJ1Result(BaseModel):
    """The join's typed result, under the graph run's correlation id.

    ``passed`` carries every required verdict and the host stage started. ``failed`` carries the typed code, the nodes
    whose verdict was missing, and ``started`` is false: no host discovery or placement ran on it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    join: str = Field(min_length=1)
    correlation_id: str = Field(min_length=1)
    state: Literal["passed", "failed"]
    started: bool
    verdicts: tuple[ModelGraphNodeVerdict, ...] = ()
    #: The required nodes the graph run carried no verdict for.
    missing: tuple[str, ...] = ()
    failure_code: str = ""
    failure_detail: str = ""

    @model_validator(mode="after")
    def _shape(self) -> ModelJoinJ1Result:
        if self.state == "passed":
            if not self.started or self.failure_code or self.missing:
                raise ValueError("a passed join started and carries no failure")
        elif self.started or not self.failure_code:
            raise ValueError("a failed join never started and carries its failure code")
        return self
