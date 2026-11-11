# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Overlay models and loader for node_lab_proof_judge_compute (OMN-23642).

Every key is required; no defaults. The overlay declares which nodes run
outside the runtime, how they are proven instead, and the test-marker
selection rules.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.nodes.node_lab_proof_judge_compute.models.enum_runs_by import EnumRunsBy

NODE_NAME = "node_lab_proof_judge_compute"
POOL_OVERLAY_ID = "prepr_runtime_pool"


class ModelSubstituteProof(BaseModel):
    """How a node is proven instead of running in the runtime."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    how: str = Field(..., min_length=1)
    host_classes: tuple[str, ...] = Field(..., min_length=1)


class ModelNotApplicableEntry(BaseModel):
    """One node that runs outside the runtime, with its substitute proof."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    node: str = Field(..., min_length=1)
    runs_by: EnumRunsBy
    reason: str = Field(..., min_length=1)
    substitute_proof: ModelSubstituteProof


class ModelNotApplicableRepo(BaseModel):
    """The not-applicable declaration for one repository."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str = Field(..., min_length=1)
    node_path_regex: str = Field(..., min_length=1)
    non_runtime_regex: tuple[str, ...]
    entries: tuple[ModelNotApplicableEntry, ...] = Field(..., min_length=1)

    @model_validator(mode="after")
    def _validate_regexes(self) -> ModelNotApplicableRepo:
        try:
            pattern = re.compile(self.node_path_regex)
        except re.error as exc:
            raise ValueError(f"node_path_regex does not compile: {exc}") from exc
        if "node" not in pattern.groupindex:
            raise ValueError("node_path_regex must have a named group `node`")
        for rx in self.non_runtime_regex:
            try:
                re.compile(rx)
            except re.error as exc:
                raise ValueError(f"non_runtime_regex does not compile: {exc}") from exc
        nodes = [e.node for e in self.entries]
        if len(nodes) != len(set(nodes)):
            raise ValueError("duplicate node in entries")
        return self


class ModelNotApplicableClass(BaseModel):
    """The not-applicable class: which rule it applies to and in which repos."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    class_id: str = Field(..., min_length=1)
    rule: str = Field(..., min_length=1)
    repos: tuple[ModelNotApplicableRepo, ...] = Field(..., min_length=1)

    @model_validator(mode="after")
    def _validate_rule(self) -> ModelNotApplicableClass:
        if self.rule != "changed_path_live":
            raise ValueError(f"rule must be changed_path_live, got {self.rule!r}")
        repos = [r.repo for r in self.repos]
        if len(repos) != len(set(repos)):
            raise ValueError("duplicate repo in not_applicable.repos")
        return self


class ModelSubstituteReceiptsConfig(BaseModel):
    """Where substitute receipts live on disk."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    root_env: str = Field(..., min_length=1)
    subdir: str = Field(..., min_length=1)


class ModelTestMarkerRepo(BaseModel):
    """The test-marker selection for one repository."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str = Field(..., min_length=1)
    broker_marker: str = Field(..., min_length=1)
    selector_without_broker: str = Field(..., min_length=1)
    source: str = Field(..., min_length=1)
    not_run_reason: str = Field(..., min_length=1)


class ModelTestMarkers(BaseModel):
    """The test-marker selection rules."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repos: tuple[ModelTestMarkerRepo, ...]

    @model_validator(mode="after")
    def _validate_repos(self) -> ModelTestMarkers:
        repos = [r.repo for r in self.repos]
        if len(repos) != len(set(repos)):
            raise ValueError("duplicate repo in test_markers.repos")
        return self


class ModelRetention(BaseModel):
    """How long the proof's log is kept beside its receipt."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    log_hours: int = Field(..., ge=1)


class ModelLabProofJudgeOverlay(BaseModel):
    """The overlay for node_lab_proof_judge_compute."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = Field(..., ge=1)
    not_applicable: ModelNotApplicableClass
    substitute_receipts: ModelSubstituteReceiptsConfig
    test_markers: ModelTestMarkers
    all_not_run_ok: bool
    retention: ModelRetention


class ModelPoolJudgeCommandOverlay(BaseModel):
    """The driver-read key that names this node's command (OMN-23715)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1]
    lab_proof_judge_command: str = Field(min_length=1)


def load_overlay(environ: Mapping[str, str] | None = None) -> ModelLabProofJudgeOverlay:
    """Read the deployment overlay at ``<root>/node_lab_proof_judge_compute/overlay.yaml`` under ``ONEX_SKILL_OVERLAY_ROOTS``.

    An absent or invalid overlay refuses; there is no fallback to the packaged example.
    """
    return load_structured_overlay(NODE_NAME, ModelLabProofJudgeOverlay, environ)


def load_pool_judge_command_overlay(
    environ: Mapping[str, str] | None = None,
) -> ModelPoolJudgeCommandOverlay:
    """Read the pool driver's judge-command overlay at ``<root>/prepr_runtime_pool/overlay.yaml`` under ``ONEX_SKILL_OVERLAY_ROOTS``.

    An absent or invalid overlay refuses; there is no fallback.
    """
    return load_structured_overlay(
        POOL_OVERLAY_ID, ModelPoolJudgeCommandOverlay, environ
    )


def load_example_overlay() -> ModelLabProofJudgeOverlay:
    """Load the shipped example overlay (for tests)."""
    example = Path(__file__).with_name("overlay.example.yaml")
    raw = yaml.safe_load(example.read_text())
    return ModelLabProofJudgeOverlay.model_validate(raw)
