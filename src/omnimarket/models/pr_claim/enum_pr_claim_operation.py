# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The operations of the PR claim registry."""

from enum import StrEnum


class EnumPrClaimOperation(StrEnum):
    ACQUIRE = "acquire"
    RELEASE = "release"
    HEARTBEAT = "heartbeat"
    CLEANUP_STALE_OWN_CLAIMS = "cleanup_stale_own_claims"
    LIST_ACTIVE = "list_active"
    HAS_ACTIVE = "has_active"
    GET_CLAIM = "get_claim"
