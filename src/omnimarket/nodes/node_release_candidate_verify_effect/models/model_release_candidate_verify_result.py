# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The verdict: the matches flag the operation owns, the reading and every named mismatch."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_release_candidate_verify_effect.models.enum_release_candidate_finding import (
    EnumReleaseCandidateFinding,
)


class ModelReleaseCandidateFinding(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: EnumReleaseCandidateFinding
    subject: str
    detail: str = ""


class ModelInstalledDistribution(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    version: str


class ModelReleaseCandidateVerifyResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    operation: str
    manifest_id: str
    matches: bool
    installed_matches_manifest: bool | None = None
    plugin_matches_manifest: bool | None = None
    reading: str
    layout: str = ""
    installed_git_commit_sha: str = ""
    installed_distributions: tuple[ModelInstalledDistribution, ...] = ()
    findings: tuple[ModelReleaseCandidateFinding, ...] = ()
