# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Why a Hostile Review provider attempt was made (OMN-20910)."""

from __future__ import annotations

from enum import StrEnum, unique


@unique
class EnumReviewCallAttemptKind(StrEnum):
    """The reason for an attempt: the first try, a JSON-only retry or a split."""

    FIRST = "first"
    JSON_ONLY_RETRY = "json_only_retry"
    MORE_SPLIT = "more_split"


__all__ = ["EnumReviewCallAttemptKind"]
