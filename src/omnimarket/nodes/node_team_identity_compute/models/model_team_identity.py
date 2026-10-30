# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The team identity declaration, its resolved source, and the role a caller resolves to.

The definitions live in :mod:`omnimarket.models.team_identity` so callers in other nodes share them
without reaching into this node's models package; this module keeps the contract's model paths.
"""

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
