# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""What the OCC companion mint does with a classified failure (OMN-15447)."""

from __future__ import annotations

from enum import StrEnum, unique


@unique
class EnumMintFailureDisposition(StrEnum):
    """The contract-declared response to an :class:`EnumMintFailureClass`.

    There are exactly two, and neither of them is "drop". A mint request that
    cannot be completed is parked with a typed reason on the dead-letter topic
    where it stays replayable; it is never discarded and never reported on the
    success topic.
    """

    # Try again, bounded by ``max_attempts`` with backoff, then park.
    RETRY = "retry"
    # Park now. Used where a fresh attempt cannot help, or actively hurts --
    # spending an exhausted API budget is the live case (OMN-15447).
    PARK = "park"
