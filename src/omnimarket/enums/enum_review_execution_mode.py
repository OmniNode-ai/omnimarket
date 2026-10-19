# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Whether a Hostile Review call ran synchronously or through a batch (OMN-20910)."""

from __future__ import annotations

from enum import StrEnum, unique


@unique
class EnumReviewExecutionMode(StrEnum):
    """The execution mode a review call was priced and run under."""

    SYNCHRONOUS = "synchronous"
    BATCH = "batch"


__all__ = ["EnumReviewExecutionMode"]
