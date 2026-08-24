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
    # OMN-16419: the configured model_name is absent from the endpoint's live
    # GET /v1/models served ids. Distinct from MODEL_UNAVAILABLE (endpoint
    # unreachable/unhealthy) — this is a reachable, healthy endpoint serving a
    # DIFFERENT model than the one attributed in delegation receipts and
    # llm-call-completed events. Fails closed rather than silently attributing
    # to a model that is not running (e.g. SGLang echoing an unknown model
    # string back at HTTP 200).
    MODEL_ATTRIBUTION_MISMATCH = "model_attribution_mismatch"
    UNKNOWN = "unknown"
