# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed answers of the tenant gateway key node: every refusal and every handled error is one of these."""

from __future__ import annotations

from enum import StrEnum


class EnumTenantApiKeyRegisterOutcome(StrEnum):
    """What a register command answered on its completed topic."""

    REGISTERED = "registered"
    ALREADY_REGISTERED = "already_registered"
    REFUSED_INVALID_KEY_HASH = "refused_invalid_key_hash"
    REFUSED_INVALID_NAME = "refused_invalid_name"
    REFUSED_EXPIRY_NOT_IN_FUTURE = "refused_expiry_not_in_future"
    REFUSED_EXPIRY_BEYOND_MAX_LIFETIME = "refused_expiry_beyond_max_lifetime"
    REFUSED_EXPIRY_REQUIRED = "refused_expiry_required"
    REFUSED_TENANT_NOT_ACTIVE = "refused_tenant_not_active"
    REFUSED_NAME_CONFLICT = "refused_name_conflict"
    REFUSED_HASH_COLLISION = "refused_hash_collision"
    REFUSED_NOT_CONFIGURED = "refused_not_configured"
    STORE_SCHEMA_OUTDATED = "store_schema_outdated"
    STORE_UNAVAILABLE = "store_unavailable"


class EnumTenantApiKeyRevokeOutcome(StrEnum):
    """What a revoke command answered on its completed topic."""

    REVOKED = "revoked"
    ALREADY_REVOKED = "already_revoked"
    NOT_FOUND = "not_found"
    STORE_UNAVAILABLE = "store_unavailable"


__all__: list[str] = [
    "EnumTenantApiKeyRegisterOutcome",
    "EnumTenantApiKeyRevokeOutcome",
]
