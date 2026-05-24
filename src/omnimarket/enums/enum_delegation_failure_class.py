# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Delegation failure class enum for LLM cost routing taxonomy."""

from __future__ import annotations

from enum import StrEnum, unique


@unique
class EnumDelegationFailureClass(StrEnum):
    """Taxonomy of failure classes for LLM delegation escalation events."""

    MODEL_UNAVAILABLE = "model_unavailable"
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    INVALID_JSON = "invalid_json"
    QUALITY_GATE_FAILED = "quality_gate_failed"
    CONTEXT_TOO_LARGE = "context_too_large"
    PRICING_UNKNOWN = "pricing_unknown"
    PROVIDER_AUTH_FAILED = "provider_auth_failed"
    UNKNOWN = "unknown"
