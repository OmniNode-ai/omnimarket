# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared immutable host reconciliation facts, decisions, command and run result."""

from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field


class ModelHostReconcileCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    correlation_id: UUID = Field(default_factory=uuid4)
    workspace_root: str = Field(min_length=1)
    mode: Literal["check", "repair"] = "repair"
    branch: str = "dev"
    target_host: str = Field(min_length=1)
    step_timeout_s: int = Field(default=1800, gt=0)
    run_timeout_s: int = Field(default=3900, gt=0)
    max_holder_age_s: int = Field(default=7200, gt=0)
    lock_stale_s: int = Field(default=3600, gt=0)
    clone_delegate: str | None = None
    venv_delegate: str | None = None
    receipt: str | None = None
    dispatch_venv: str | None = None
    ledger: str = ""
    alert_command: str = ""


class ModelHostReconcileSlackCommand(BaseModel):
    """Mirror the publish node's command model for the emitted wire fields.

    This is a mirror rather than an import because nodes communicate over the
    bus and must not import another node's private model package. A drift test
    validates the payload against the publish node's command model.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    channel: str = Field(min_length=1)
    text: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    correlation_id: UUID


class ModelSurfaceMovementFact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["movement"] = "movement"
    surface: str
    owner: str = ""
    before: str = ""
    after: str = ""
    target: str = ""


class ModelSurfaceUnhealthyFact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["unhealthy"] = "unhealthy"
    surface: str
    owner: str = ""
    reason: str


class ModelSurfaceUncoveredFact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["uncovered"] = "uncovered"
    surface: str
    owner: str = ""
    delegate: str
    layer: Literal["clone", "venv"]


class ModelSurfaceIndeterminateFact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["indeterminate"] = "indeterminate"
    surface: str
    owner: str = ""
    detail: str


class ModelGatePurityFact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["gate_purity"] = "gate_purity"
    surface: Literal["venv:gate-purity"] = "venv:gate-purity"
    owner: str = ""
    gate_venv: str
    provider: str = ""
    dispatch_venv: str


class ModelPathShadowFact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["path_shadow"] = "path_shadow"
    surface: Literal["onex-path-shadow"] = "onex-path-shadow"
    owner: str = ""
    shadow: str
    wrapper: str
    wrapper_resolved: str
    is_symlink: bool
    resolved: str
    points_to_wrapper: bool


class ModelGuardFact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["guard"] = "guard"
    surface: Literal["canonical-guard"] = "canonical-guard"
    owner: str = ""
    live_dir: str
    source_dir: str
    checked: int
    drifted: tuple[str, ...]


ModelSurfaceFact = Annotated[
    ModelSurfaceMovementFact
    | ModelSurfaceUnhealthyFact
    | ModelSurfaceUncoveredFact
    | ModelSurfaceIndeterminateFact
    | ModelGatePurityFact
    | ModelPathShadowFact
    | ModelGuardFact,
    Field(discriminator="kind"),
]


class ModelHostReconcileEvaluateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    mode: Literal["check", "repair"]
    workspace_root: str
    scripts_dir: str
    rerun_command: str
    receipt_path: str
    facts: tuple[ModelSurfaceFact, ...] = ()


class ModelSurfaceDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    surface: str
    verdict: str
    dispatch_premise: bool
    remedy: str = ""
    owner: str = ""
    detail: str


class ModelHostReconcileDecisions(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    surfaces: tuple[ModelSurfaceDecision, ...]
    failures: int
    dispatch_premise_failures: int
    exit_code: Literal[0, 2]
    stamp_floor: bool
    verdict_lines: tuple[str, ...]


class ModelHostReconcileRunResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    exit_code: Literal[0, 2, 3, 4, 5, 6]
    host: str
    correlation_id: UUID
    surfaces: tuple[ModelSurfaceDecision, ...] = ()
    diagnostics: tuple[str, ...] = ()
    floor_stamped: bool = False
