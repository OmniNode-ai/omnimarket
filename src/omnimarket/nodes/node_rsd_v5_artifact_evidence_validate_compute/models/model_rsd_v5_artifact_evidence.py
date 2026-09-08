# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contracts for pure V5 worker-evidence closure validation."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.rsd.container_bootstrap_artifact_evidence_v5 import (
    ContainerBootstrapArtifactEvidenceAcceptanceV5,
    ContainerBootstrapArtifactEvidenceClosureV5,
    ContainerBootstrapBuildWorkerTrustPolicyV5,
)
from omnimarket.rsd.v4_static_profile import (
    ContainerBootstrapStaticProfileTrustAnchorV4,
    ContainerBootstrapStaticRoleProfileEnvelopeV4,
)


class ModelRsdV5ArtifactEvidenceInput(BaseModel):
    """Supplied, already-collected V5 closure and pinned verification inputs."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    closure: ContainerBootstrapArtifactEvidenceClosureV5
    worker_trust_policy: ContainerBootstrapBuildWorkerTrustPolicyV5
    profile_envelope: ContainerBootstrapStaticRoleProfileEnvelopeV4
    profile_trust_anchor: ContainerBootstrapStaticProfileTrustAnchorV4


class ModelRsdV5ArtifactEvidenceOutput(BaseModel):
    """A non-authorizing V5 acceptance projection with no effect surface."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    acceptance: ContainerBootstrapArtifactEvidenceAcceptanceV5
    closure_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    verification_context_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    schema_version: Literal["rsd.container-bootstrap-artifact-evidence-acceptance.v5"]
    non_authorizing: Literal[True] = True
    evidence_effect_allowed: Literal[False] = False
    build_allowed: Literal[False] = False
    materialization_allowed: Literal[False] = False
    attach_allowed: Literal[False] = False
