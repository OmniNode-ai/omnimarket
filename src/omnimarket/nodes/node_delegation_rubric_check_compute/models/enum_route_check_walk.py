# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The walk a receipt is checked against; the wire values stay "L" and "C"."""

from enum import StrEnum


class EnumRouteCheckWalk(StrEnum):
    LOCAL = "L"
    CUSTOMER_KEY = "C"
