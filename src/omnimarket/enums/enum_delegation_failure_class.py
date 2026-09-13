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
    # OMN-18296: the process that owned an in-flight leg went away and the leg
    # was lost with it. Distinct from TIMEOUT, which is a provider that took too
    # long to answer a call we can still see: here no call is outstanding at all
    # — the inference command's consumer offset was already committed, so it is
    # never redelivered, and no response event, success or failure, will ever
    # arrive. Measured on the lab lane 2026-09-13: the effects pod was recreated
    # at 09:57:40Z with correlation a2fe0848 in flight since 09:55:04Z; the
    # request sat on the bus with no matching response, the FSM row stayed
    # ROUTED/in_flight, and the customer's delegation never terminalised.
    RUNTIME_RESTART_DURING_DELEGATION = "runtime_restart_during_delegation"
    UNKNOWN = "unknown"
