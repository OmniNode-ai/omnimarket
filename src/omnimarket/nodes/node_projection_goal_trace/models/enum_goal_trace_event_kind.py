# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The kinds of event a goal trace folds (OMN-20034, GC.11)."""

from __future__ import annotations

from enum import StrEnum


class EnumGoalTraceEventKind(StrEnum):
    """One value per source event the trace absorbs.

    The declaration order is the canonical rank used to order the trace, so a
    new kind is appended and never inserted.
    """

    CLAIM = "claim"
    REVISE = "revise"
    CLOSE = "close"
    BIND = "bind"
    HOOK = "hook"
    DELEGATION = "delegation"
    RECEIPT = "receipt"
    ATTEMPT = "attempt"


__all__ = ["EnumGoalTraceEventKind"]
