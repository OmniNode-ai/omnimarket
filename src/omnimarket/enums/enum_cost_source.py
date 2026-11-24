# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Where the cost figure of a delegation call came from."""

from __future__ import annotations

from enum import StrEnum, unique


@unique
class EnumCostSource(StrEnum):
    """Which figure a call's cost is.

    REPORTED: the charge the provider itself stated in its response usage
        accounting, copied as it came.
    ESTIMATED: the provider's token counts multiplied by a published price
        (the provider's own list, a kept models.dev copy, or the price list
        shipped in the install), because the response stated no charge.
    """

    REPORTED = "reported"
    ESTIMATED = "estimated"
