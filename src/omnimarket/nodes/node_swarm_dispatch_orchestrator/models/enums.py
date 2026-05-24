# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Enums for swarm dispatch orchestrator node."""

from __future__ import annotations

from enum import StrEnum


class EnumSwarmOrchestratorState(StrEnum):
    RECEIVED = "received"
    HEALTH_CHECKED = "health_checked"
    DECOMPOSED = "decomposed"
    ENDPOINTS_SELECTED = "endpoints_selected"
    DISPATCHING = "dispatching"
    AGGREGATING = "aggregating"
    COMPLETED = "completed"
    FAILED = "failed"


class EnumSwarmRunStatus(StrEnum):
    SUCCEEDED = "succeeded"
    DEGRADED = "degraded"
    FAILED = "failed"
    TIMEOUT = "timeout"


class EnumSubtaskStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMEOUT = "timeout"
    SKIPPED_DEPENDENCY_FAILED = "skipped_dependency_failed"
    CONTEXT_WINDOW_EXCEEDED = "context_window_exceeded"


class EnumDecompositionStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED_FALLBACK_PASSTHROUGH = "failed_fallback_passthrough"
    PASSTHROUGH_TOKEN_THRESHOLD = "passthrough_token_threshold"
    PASSTHROUGH_CALLER_DISABLED = "passthrough_caller_disabled"


class EnumAggregationMode(StrEnum):
    CONCATENATION = "concatenation"
    SYNTHESIS = "synthesis"
