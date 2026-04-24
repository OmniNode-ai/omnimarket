# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared enums for intelligence ONCP nodes."""

from enum import StrEnum


class EnumFSMType(StrEnum):
    """FSM workflows handled by intelligence reducer nodes."""

    INGESTION = "INGESTION"
    PATTERN_LEARNING = "PATTERN_LEARNING"
    QUALITY_ASSESSMENT = "QUALITY_ASSESSMENT"
    PATTERN_LIFECYCLE = "PATTERN_LIFECYCLE"


class EnumOrchestratorWorkflowType(StrEnum):
    """High-level intelligence workflow operations."""

    DOCUMENT_INGESTION = "DOCUMENT_INGESTION"
    PATTERN_LEARNING = "PATTERN_LEARNING"
    QUALITY_ASSESSMENT = "QUALITY_ASSESSMENT"
    SEMANTIC_ANALYSIS = "SEMANTIC_ANALYSIS"
    RELATIONSHIP_DETECTION = "RELATIONSHIP_DETECTION"


class EnumPatternLifecycleStatus(StrEnum):
    """Pattern lifecycle status values."""

    CANDIDATE = "candidate"
    PROVISIONAL = "provisional"
    VALIDATED = "validated"
    DEPRECATED = "deprecated"


class EnumRunResult(StrEnum):
    """Pipeline run result values."""

    SUCCESS = "success"
    PARTIAL = "partial"
    FAILURE = "failure"


__all__ = [
    "EnumFSMType",
    "EnumOrchestratorWorkflowType",
    "EnumPatternLifecycleStatus",
    "EnumRunResult",
]
