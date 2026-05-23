# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Cost basis enum for LLM delegation cost attribution."""

from __future__ import annotations

from enum import StrEnum, unique


@unique
class EnumCostBasis(StrEnum):
    """Cost basis for LLM delegation calls.

    CLOUD_API_COST: Metered cloud API cost incurred per call.
    ZERO_MARGINAL_API_COST: No per-call API charge (local or free-tier);
        does not mean inference is free (electricity/hardware not included).
    UNKNOWN: Cost basis could not be determined.
    """

    CLOUD_API_COST = "cloud_api_cost"
    ZERO_MARGINAL_API_COST = "zero_marginal_api_cost"
    UNKNOWN = "unknown"
