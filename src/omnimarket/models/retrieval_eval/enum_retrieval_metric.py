# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Retrieval metric identifiers (OMN-21132, plan task TC.9, port item AP-E1)."""

from enum import StrEnum


class EnumRetrievalMetric(StrEnum):
    """Canonical retrieval metrics."""

    RECALL_AT_K = "recall_at_k"
    PRECISION_AT_K = "precision_at_k"
    AVERAGE_PRECISION = "average_precision"
    RECIPROCAL_RANK = "reciprocal_rank"
