# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contracts for pure B2 V2 artifact-manifest validation."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.rsd.b1_projection_binding import (
    TargetDeliveryMapProjectionBindingTrustPolicyV1,
)
from omnimarket.rsd.historical_target_delivery_map_v1 import TargetDeliveryMapV1
from omnimarket.rsd.target_delivery_artifact_manifest_trust_anchor_v1 import (
    TargetDeliveryArtifactManifestTrustAnchorV1,
)
from omnimarket.rsd.target_delivery_artifact_manifest_v2 import (
    TargetDeliveryArtifactManifestAcceptanceV2,
    TargetDeliveryArtifactManifestRoleInputV2,
    TargetDeliveryArtifactManifestV2,
    TargetDeliveryArtifactManifestV5RolePolicyInputV2,
)
from omnimarket.rsd.v4_static_profile import (
    ContainerBootstrapStaticDeliveryProjectionV4,
)


class ModelRsdTargetDeliveryArtifactManifestV2Input(BaseModel):
    """Only original, caller-supplied evidence and pinned public roots."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    delivery_map: TargetDeliveryMapV1
    static_delivery_projection: ContainerBootstrapStaticDeliveryProjectionV4
    b1_trust_policy: TargetDeliveryMapProjectionBindingTrustPolicyV1
    manifest_trust_anchor: TargetDeliveryArtifactManifestTrustAnchorV1
    role_inputs: tuple[
        TargetDeliveryArtifactManifestRoleInputV2,
        TargetDeliveryArtifactManifestRoleInputV2,
        TargetDeliveryArtifactManifestRoleInputV2,
        TargetDeliveryArtifactManifestRoleInputV2,
    ]
    v5_role_policy_inputs: tuple[
        TargetDeliveryArtifactManifestV5RolePolicyInputV2,
        TargetDeliveryArtifactManifestV5RolePolicyInputV2,
        TargetDeliveryArtifactManifestV5RolePolicyInputV2,
        TargetDeliveryArtifactManifestV5RolePolicyInputV2,
    ]
    manifest: TargetDeliveryArtifactManifestV2


class ModelRsdTargetDeliveryArtifactManifestV2Output(BaseModel):
    """A diagnostic acceptance with no authority or effect surface."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    acceptance: TargetDeliveryArtifactManifestAcceptanceV2
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    verification_context_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    schema_version: Literal["rsd.target-delivery-artifact-manifest-acceptance.v2"]
    non_authorizing: Literal[True] = True
    evidence_effect_allowed: Literal[False] = False
    build_allowed: Literal[False] = False
    materialization_allowed: Literal[False] = False
    attach_allowed: Literal[False] = False
    effect_allowed: Literal[False] = False
