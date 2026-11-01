# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Shared literal types for evidence pipeline DTOs."""

from __future__ import annotations

from typing import Literal

type EvidenceLifecycleState = Literal[
    "PROVISIONAL",
    "VALIDATED",
    "FINALIZED",
    "SUPERSEDED",
    "REJECTED",
]

type GapClassification = Literal[
    "MISSING",
    "STALE",
    "SUPERSEDED",
    "HASH_MISMATCH",
    "RECEIPT_MISSING",
    "VALIDATION_FAILED",
    "UNKNOWN",
]

type ReadinessState = Literal[
    "READY",
    "BLOCKED",
    "DEGRADED",
    "ADVISORY_ONLY",
]

type DashboardSeverity = Literal[
    "INFO",
    "WARNING",
    "ERROR",
    "BLOCKING",
]

type DashboardStage = Literal[
    "TRIGGERED",
    "COLLECTED",
    "EXTRACTED",
    "VALIDATED",
    "OCC_PR",
    "COMPLETED",
    "BLOCKED",
    "READINESS_GATE_STARTED",
    "READINESS_GATE_COMPLETED",
    "READINESS_GATE_BLOCKED",
]

type DashboardStatus = Literal[
    "PENDING",
    "IN_FLIGHT",
    "PASSED",
    "FAILED",
    "BLOCKED",
    "STALE",
    "DEGRADED",
]

type FreshnessState = Literal[
    "CURRENT",
    "STALE",
    "DEGRADED",
]

type MissingEventClassification = Literal[
    "DELAYED",
    "STALE",
    "MISSING",
    "SUPERSEDED",
    "INVALID_ORDER",
    "PROJECTION_GAP",
]

type TriggerSurface = Literal[
    "pull_request_merge",
    "merge_group",
    "deploy",
    "manual",
    "runtime_event",
]

type ValidationState = Literal[
    "PASSED",
    "FAILED",
    "ADVISORY_ONLY",
]

__all__: list[str] = [
    "DashboardSeverity",
    "DashboardStage",
    "DashboardStatus",
    "EvidenceLifecycleState",
    "FreshnessState",
    "GapClassification",
    "MissingEventClassification",
    "ReadinessState",
    "TriggerSurface",
    "ValidationState",
]
