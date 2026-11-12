# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed failures of the code eval suite census (OMN-23883)."""

from __future__ import annotations

from enum import StrEnum, unique


@unique
class EnumSuiteCensusFailure(StrEnum):
    """Why a census request ended as a typed failure instead of counts.

    A failure census carries the suite id and a detail and nothing else: no
    candidates, no counts, and a digest of the empty string rather than a
    missing one.
    """

    NO_TASKS = "no_tasks"
    DUPLICATE_TASK = "duplicate_task"
    OVERLAY_UNAVAILABLE = "overlay_unavailable"


__all__ = ["EnumSuiteCensusFailure"]
