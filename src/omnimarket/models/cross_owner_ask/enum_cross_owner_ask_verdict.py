# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The verdict on a request drafted to another owner about that owner's repository."""

from __future__ import annotations

from enum import StrEnum, unique


@unique
class EnumCrossOwnerAskVerdict(StrEnum):
    """Only ABSENT lets the draft go on; every other verdict refuses it."""

    ABSENT = "absent"
    PRESENT = "present"
    UNREADABLE = "unreadable"
    NONCANONICAL_READ = "noncanonical_read"
