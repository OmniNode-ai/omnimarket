# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay of node_validator_workflow_orchestrator: every setting, none in code (OMN-23078).

Found at ``<root>/node_validator_workflow_orchestrator/overlay.yaml`` under a root named in
``ONEX_SKILL_OVERLAY_ROOTS``; the package ships ``overlay.example.yaml``. No key has a default.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from omnimarket.handlers.node_overlay_reader import load_structured_overlay

NODE_NAME = "node_validator_workflow_orchestrator"
EXAMPLE = Path(__file__).with_name("overlay.example.yaml")


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ModelValidatorEntry(_Frozen):
    validator_id: str = Field(min_length=1)
    node_package: str = Field(min_length=1)
    validation_contract_package: str = Field(min_length=1)
    validation_contract_file: str = Field(min_length=1)
    input_kind: str = Field(min_length=1)


class ModelCanonicalRequirement(_Frozen):
    """What a validator's node contract must declare to count as canonical."""

    node_type: str = Field(min_length=1)
    purity: str = Field(min_length=1)
    tag: str = Field(min_length=1)


class ModelRatchetSource(_Frozen):
    key: str = Field(min_length=1)
    path: str = Field(min_length=1)
    comment_prefix: str = Field(min_length=1)


class ModelGateSetting(_Frozen):
    check_name: str = Field(min_length=1)


class ModelBackendSetting(_Frozen):
    reader: Literal["in_memory", "git"]
    emitter: Literal["in_memory", "file"]
    gate: Literal["in_memory", "file"]


class ModelGitSetting(_Frozen):
    repo_root_env: str = Field(min_length=1)
    cat_file_timeout_s: float = Field(gt=0)


class ModelFileSetting(_Frozen):
    trace_path_env: str = Field(min_length=1)
    gate_path_env: str = Field(min_length=1)


class ModelValidatorWorkflowOverlay(_Frozen):
    validators: tuple[ModelValidatorEntry, ...] = Field(min_length=1)
    canonical: ModelCanonicalRequirement
    ratchet_sources: tuple[ModelRatchetSource, ...]
    gate: ModelGateSetting
    backends: ModelBackendSetting
    git: ModelGitSetting
    files: ModelFileSetting


def load_overlay(
    environ: Mapping[str, str] | None = None, explicit: object | None = None
) -> ModelValidatorWorkflowOverlay:
    return load_structured_overlay(
        NODE_NAME, ModelValidatorWorkflowOverlay, environ, explicit=explicit
    )


def example_overlay() -> ModelValidatorWorkflowOverlay:
    return ModelValidatorWorkflowOverlay.model_validate(
        yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    )
