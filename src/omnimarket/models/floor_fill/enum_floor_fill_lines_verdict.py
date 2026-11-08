# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The verdict of one floor line, as the candidates compute (T1) names it."""

from __future__ import annotations

from enum import StrEnum


class EnumFloorFillLinesVerdict(StrEnum):
    """`UNTICKETABLE` is a line whose ready tasks are all untickable: nothing else to start."""

    STARTABLE = "STARTABLE"
    NEEDS_TICKET = "NEEDS-TICKET"
    UNTICKETABLE = "UNTICKETABLE"
    AT_CAPACITY = "AT-CAPACITY"
    NO_STARTABLE = "NO-STARTABLE"
