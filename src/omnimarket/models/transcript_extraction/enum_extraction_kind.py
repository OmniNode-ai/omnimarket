# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What a proposed extraction claims the operator gave (OMN-21139, plan task TE.2)."""

from enum import StrEnum


class EnumExtractionKind(StrEnum):
    """Role A proposes these four kinds, plan section 5.1."""

    RULING = "ruling"
    CORRECTION = "correction"
    CONSTRAINT = "constraint"
    DECISION = "decision"
