# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contracts for pure V5 OCI safe-config validation."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.rsd.container_bootstrap_oci_safe_config_evidence_v5 import (
    ContainerBootstrapOciSafeConfigEvidenceV5,
)


class ModelRsdV5OciSafeConfigInput(BaseModel):
    """Already-collected evidence plus transient canonical OCI config bytes."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    evidence: ContainerBootstrapOciSafeConfigEvidenceV5
    raw_config_json: bytes = Field(min_length=1, max_length=65_536)


class ModelRsdV5OciSafeConfigOutput(BaseModel):
    """Non-authorizing commitment result; no runtime or effect fields exist."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    config_commitment_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    schema_version: Literal["rsd.container-bootstrap-oci-safe-config-evidence.v5"]
    non_authorizing: Literal[True] = True
    evidence_effect_allowed: Literal[False] = False
    build_allowed: Literal[False] = False
    materialization_allowed: Literal[False] = False
    attach_allowed: Literal[False] = False
