"""Shared verdict enum for the plan first-slice check.

Lives in the shared enums package so the first-slice and review-ladder nodes
both use it without importing each other's model packages.
"""

from __future__ import annotations

from enum import StrEnum


class EnumPlanFirstSliceVerdict(StrEnum):
    """What the plan's first-slice check found."""

    OK = "ok"
    NO_FIRST_SLICE_SECTION = "no_first_slice_section"
    EMPTY_FIRST_SLICE_SECTION = "empty_first_slice_section"


__all__: list[str] = ["EnumPlanFirstSliceVerdict"]
