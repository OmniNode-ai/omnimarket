"""Enums for swarm registry compute node."""

from __future__ import annotations

from enum import StrEnum


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
]
