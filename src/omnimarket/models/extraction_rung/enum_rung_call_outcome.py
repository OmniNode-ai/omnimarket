# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""How one rung call ended in the extraction harness (OMN-21140, plan task TE.3)."""

from enum import StrEnum


class EnumRungCallOutcome(StrEnum):
    """Parse failures and misses are separate outcomes and separate counts.

    HIT: the output parsed and every expected quote was proposed.
    MISS: the output parsed, and an expected quote was not proposed.
    PARSE_FAILURE: nothing recoverable parsed, or a proposal is not a valid event.
    RECORDING_CORRUPT: the recorded bytes do not match the recorded digest.
    """

    HIT = "hit"
    MISS = "miss"
    PARSE_FAILURE = "parse_failure"
    RECORDING_CORRUPT = "recording_corrupt"
