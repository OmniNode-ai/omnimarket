# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
from __future__ import annotations

from enum import StrEnum


class EnumSwarmCapability(StrEnum):
    CODE_GENERATION = "code_generation"
    STRUCTURED_OUTPUT = "structured_output"
    REFACTORING = "refactoring"
    REASONING = "reasoning"
    ANALYSIS = "analysis"
    MATH = "math"
    PLANNING = "planning"
    SYNTHESIS = "synthesis"
    GENERAL = "general"


class EnumSubtaskCategory(StrEnum):
    CODE = "code"
    REASONING = "reasoning"
    SYNTHESIS = "synthesis"
    ANALYSIS = "analysis"
    PLANNING = "planning"
    GENERAL = "general"


class EnumDecompositionStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED_FALLBACK_PASSTHROUGH = "failed_fallback_passthrough"
    PASSTHROUGH_TOKEN_THRESHOLD = "passthrough_token_threshold"
    PASSTHROUGH_CALLER_DISABLED = "passthrough_caller_disabled"


class EnumSubtaskStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMEOUT = "timeout"
    SKIPPED_DEPENDENCY_FAILED = "skipped_dependency_failed"
    CONTEXT_WINDOW_EXCEEDED = "context_window_exceeded"
