# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The kind of delegated item inside a task class."""

from __future__ import annotations

from enum import StrEnum


class EnumComparisonKind(StrEnum):
    """Closed set, the kinds the judged-acceptance fold also keeps apart."""

    TASK = "task"
    EDIT_LOOP_TURN = "edit_loop_turn"
