"""Board probe-result outcome vocabulary (OMN-19937)."""

from enum import StrEnum


class EnumBoardProbeOutcome(StrEnum):
    """The three outcomes a board probe may assert."""

    PASS = "PASS"
    FAIL = "FAIL"
    INDETERMINATE = "INDETERMINATE"


__all__ = ["EnumBoardProbeOutcome"]
