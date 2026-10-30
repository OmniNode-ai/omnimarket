# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The team identity declaration's shared types (owned by ``node_team_identity_compute``)."""

from omnimarket.models.team_identity.model_team_identity import (
    TEAM_IDENTITY_SCHEMA_VERSION,
    ClaimSource,
    ModelIdentityClaim,
    ModelResolvedTeamIdentity,
    ModelTeamIdentityEntry,
    ModelTeamIdentityOverlay,
    ModelTeamIdentityRequest,
    ModelTeamIdentityResult,
    ModelTeamIdentitySource,
    NonEmpty,
    TeamPrivileges,
    TeamRole,
)

__all__ = [
    "TEAM_IDENTITY_SCHEMA_VERSION",
    "ClaimSource",
    "ModelIdentityClaim",
    "ModelResolvedTeamIdentity",
    "ModelTeamIdentityEntry",
    "ModelTeamIdentityOverlay",
    "ModelTeamIdentityRequest",
    "ModelTeamIdentityResult",
    "ModelTeamIdentitySource",
    "NonEmpty",
    "TeamPrivileges",
    "TeamRole",
]
