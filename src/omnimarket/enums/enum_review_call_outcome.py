# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""How one provider attempt of a Hostile Review call ended (OMN-20910)."""

from __future__ import annotations

from enum import StrEnum, unique


@unique
class EnumReviewCallOutcome(StrEnum):
    """The outcome class of one provider attempt, as the cost record states it."""

    OK = "ok"
    """The provider answered and the answer was finished."""

    TRUNCATED = "truncated"
    """The provider stopped on its output-token budget before the answer finished."""

    SCHEMA_INVALID = "schema_invalid"
    """The answer arrived but did not satisfy the declared response schema."""

    OVERFLOW = "overflow"
    """The prompt did not fit the model's context window."""

    UPSTREAM_ERROR = "upstream_error"
    """The provider, its network path or its key refused or failed the attempt."""

    CANCELLED = "cancelled"
    """The invocation ended (deadline or cancellation) while the attempt was in flight."""


__all__ = ["EnumReviewCallOutcome"]
