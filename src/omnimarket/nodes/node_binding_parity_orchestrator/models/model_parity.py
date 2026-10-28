# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Models of one parity run: the scenario, one profile's run and the comparison."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from omnimarket.nodes.node_binding_parity_orchestrator.overlay import (
    ModelFactoryBindingsOverlay,
)


class ModelScenarioRow(BaseModel):
    """One written row. ``provenance`` is validated on its own and left out of the comparison."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str
    content: str
    provenance: dict[str, str] = Field(default_factory=dict)


class ModelScenarioRun(BaseModel):
    """What one scenario produced under one profile."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    profile_id: str
    binding: str
    proof_eligible: bool
    verdicts: tuple[str, ...] = ()
    lines: tuple[str, ...] = ()
    rows: tuple[ModelScenarioRow, ...] = ()
    counts: dict[str, int] = Field(default_factory=dict)
    provenance: dict[str, str] = Field(default_factory=dict)


class ModelRunComparison(BaseModel):
    """The baseline run against one other run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    baseline_profile: str
    other_profile: str
    passed: bool
    first_difference: str = ""
    provenance_errors: tuple[str, ...] = ()


class ModelCompareResult(BaseModel):
    """Every comparison of a parity run and the first difference found."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    passed: bool
    comparisons: tuple[ModelRunComparison, ...]
    first_difference: str = ""


class ModelParityScenario(BaseModel):
    """What to run: the own stub scenario, or one operation of a node on the in-process bus."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["stub_rows", "node_operation"]
    node: str = ""
    operation: str = ""
    fixture: dict[str, JsonValue] = Field(default_factory=dict)
    allowed_differences: tuple[str, ...] = ()


class ModelParityRequest(BaseModel):
    """Facts for ``run_parity``: the scenario, the loaded overlay and the fixed clock."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    scenario: ModelParityScenario
    overlay: ModelFactoryBindingsOverlay
    fixed_now: datetime


class ModelParityResult(BaseModel):
    """The runs, the comparison and the steps of the contract's graph that were walked."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    passed: bool
    runs: tuple[ModelScenarioRun, ...]
    comparison: ModelCompareResult
    first_difference: str = ""
    steps: tuple[str, ...] = ()
