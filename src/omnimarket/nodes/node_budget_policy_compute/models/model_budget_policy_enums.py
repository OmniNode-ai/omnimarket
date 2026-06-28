"""Enums for the budget policy compute node."""

from __future__ import annotations

from enum import StrEnum


class EnumTaskPriority(StrEnum):
    LOW = "LOW"
    NORMAL = "NORMAL"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


__all__ = ["EnumTaskPriority"]
