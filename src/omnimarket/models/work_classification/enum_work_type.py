# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""EnumWorkType: the one task-class vocabulary any plan uses (closed enum)."""

from __future__ import annotations

from enum import StrEnum


class EnumWorkType(StrEnum):
    """Work types. UNDETERMINED is a value and never a default."""

    BUILD = "build"
    FIX = "fix"
    PLAN = "plan"
    DIAGNOSE = "diagnose"
    REVIEW = "review"
    LAND = "land"
    OPS = "ops"
    DOCS = "docs"
    TRIAGE = "triage"
    RESEARCH = "research"
    UNDETERMINED = "undetermined"


__all__ = ["EnumWorkType"]
