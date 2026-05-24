"""Enums for swarm registry compute node."""

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


class EnumEndpointStatus(StrEnum):
    reachable = "reachable"
    unreachable = "unreachable"
    timeout = "timeout"


class EnumModelStatus(StrEnum):
    available = "available"
    unavailable = "unavailable"
    unknown = "unknown"


__all__ = [
    "EnumEndpointStatus",
    "EnumModelStatus",
    "EnumSwarmCapability",
]
