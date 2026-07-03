"""Enums for node_projection_mcp_tools."""

from __future__ import annotations

from enum import StrEnum


class EnumMcpToolStatus(StrEnum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    REJECTED = "rejected"


class EnumFreshnessState(StrEnum):
    FRESH = "fresh"
    STALE = "stale"
    DEGRADED = "degraded"


__all__: list[str] = [
    "EnumFreshnessState",
    "EnumMcpToolStatus",
]
