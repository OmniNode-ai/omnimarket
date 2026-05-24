"""Enums for node_projection_swarm."""

from __future__ import annotations

from enum import StrEnum


class EnumSwarmRunStatus(StrEnum):
    SUCCEEDED = "succeeded"
    DEGRADED = "degraded"
    FAILED = "failed"
    TIMEOUT = "timeout"


class EnumFreshnessState(StrEnum):
    FRESH = "fresh"
    STALE = "stale"
    DEGRADED = "degraded"


__all__: list[str] = [
    "EnumFreshnessState",
    "EnumSwarmRunStatus",
]
