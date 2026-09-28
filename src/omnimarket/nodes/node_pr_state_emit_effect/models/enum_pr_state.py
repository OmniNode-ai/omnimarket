# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""PR lifecycle vocabulary."""

from enum import StrEnum


class EnumPrState(StrEnum):
    OPEN = "open"
    CLOSED = "closed"
    MERGED = "merged"
