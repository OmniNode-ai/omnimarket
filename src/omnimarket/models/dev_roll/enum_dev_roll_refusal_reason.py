# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Why a whole facts record was refused."""

from enum import StrEnum


class EnumDevRollRefusalReason(StrEnum):
    """The facts contradict themselves; no decision is made from them."""

    INCONSISTENT_LAST_ROLL = "inconsistent_last_roll"
