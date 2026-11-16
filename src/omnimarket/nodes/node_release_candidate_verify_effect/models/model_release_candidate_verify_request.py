# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The typed request of the release candidate verifier: one operation over one manifest."""

from __future__ import annotations

from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.nodes.node_release_candidate_verify_effect.models.model_release_candidate_manifest import (
    ModelManifestRecordEntry,
    ModelReleaseCandidateManifest,
)
from omnimarket.nodes.node_release_candidate_verify_effect.models.model_release_candidate_overlay import (
    ModelReleaseCandidateOverlay,
)


class ModelInstalledPluginRecord(BaseModel):
    """One plugin entry of Claude Code's installed_plugins.json."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str
    install_path: str = Field(min_length=1)
    git_commit_sha: str = ""


class ModelPypiFact(BaseModel):
    """What PyPI serves for one wheel file and the RECORD inside that downloaded wheel."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    filename: str = Field(min_length=1)
    sha256: str
    record: tuple[ModelManifestRecordEntry, ...]


class ModelAncestryFact(BaseModel):
    """The exit of git merge-base --is-ancestor for one required change against the candidate tag."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str = Field(min_length=1)
    sha: str = Field(min_length=7)
    is_ancestor: bool


class ModelReleaseCandidateVerifyRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(default_factory=uuid4)
    operation: Literal[
        "verify_installed", "verify_plugin", "verify_pypi", "verify_ancestry"
    ]
    manifest: ModelReleaseCandidateManifest
    environment_prefix: str | None = Field(
        default=None,
        description="The environment a proof installed into (verify_installed).",
    )
    onex_on_path: str | None = Field(
        default=None,
        description="What command -v onex printed in that proof (verify_installed).",
    )
    plugin_record: ModelInstalledPluginRecord | None = None
    pypi_facts: tuple[ModelPypiFact, ...] = ()
    ancestry_facts: tuple[ModelAncestryFact, ...] = ()
    overlay: ModelReleaseCandidateOverlay | None = Field(
        default=None, description="None reads the packaged overlay.default.yaml."
    )

    @model_validator(mode="after")
    def _operation_inputs(self) -> ModelReleaseCandidateVerifyRequest:
        if self.operation == "verify_installed" and not self.environment_prefix:
            raise ValueError("verify_installed needs environment_prefix")
        if self.operation == "verify_plugin" and self.plugin_record is None:
            raise ValueError("verify_plugin needs plugin_record")
        return self
