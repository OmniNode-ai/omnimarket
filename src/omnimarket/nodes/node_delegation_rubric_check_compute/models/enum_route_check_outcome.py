# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Route evidence outcomes, distinct from delegated answer correctness."""

from enum import StrEnum


class EnumRouteCheckOutcome(StrEnum):
    VALID = "VALID"
    INVALID_ROUTE = "INVALID_ROUTE"
    NO_TERMINAL = "NO_TERMINAL"
    REFUSED = "REFUSED"
