# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed request and plan for the morning-workflows installer's copy decision (OMN-24424)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ModelMorningInstallArtifact(BaseModel):
    """One file the installer copies: a bin program or the package lib."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    kind: str
    source: str
    destination: str

    @field_validator("kind")
    @classmethod
    def _kind_is_bin_or_lib(cls, value: str) -> str:
        if value not in ("bin", "lib"):
            raise ValueError('kind is "bin" or "lib"')
        return value


class ModelMorningInstallModeRule(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    artifact_kinds: tuple[str, ...]
    reload_labels: bool = False


class ModelMorningInstallPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    modes: dict[str, ModelMorningInstallModeRule]


class ModelMorningInstallRequest(BaseModel):
    """The compute's whole input: the mode, the artifacts to copy and the mode rules.

    The policy is read from the packaged overlay morning_install_policy.yaml and handed in
    by the caller; this node never reads a file (OMN-24424).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(min_length=1)
    mode: str
    artifacts: tuple[ModelMorningInstallArtifact, ...]
    policy: ModelMorningInstallPolicy


class ModelMorningInstallPlan(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    outcome: Literal["planned", "refused"]
    mode: str
    copy_actions: tuple[ModelMorningInstallArtifact, ...] = ()
    reload_labels: bool = False
    refusal_code: str = ""
    detail: str = ""
