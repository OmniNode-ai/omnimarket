# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Mode a client call runs in, ordered observe < selective < required."""

from __future__ import annotations

from enum import StrEnum, unique


@unique
class EnumDelegationClientMode(StrEnum):
    """How far a client may rely on delegation for one call.

    ``observe`` never dispatches, ``selective`` dispatches an admitted call and
    lets the client fall back to native, ``required`` refuses when delegation
    does not answer. The effective mode of a call is the lesser of the client's
    own maximum and the server cohort's mode for its host.
    """

    OBSERVE = "observe"
    SELECTIVE = "selective"
    REQUIRED = "required"

    @property
    def rank(self) -> int:
        return _RANK[self]

    def at_most(self, other: EnumDelegationClientMode) -> bool:
        """True when this mode is no higher than ``other``."""
        return self.rank <= other.rank

    @classmethod
    def lesser(
        cls, first: EnumDelegationClientMode, second: EnumDelegationClientMode
    ) -> EnumDelegationClientMode:
        """The lower of two modes."""
        return first if first.at_most(second) else second


_RANK: dict[EnumDelegationClientMode, int] = {
    EnumDelegationClientMode.OBSERVE: 0,
    EnumDelegationClientMode.SELECTIVE: 1,
    EnumDelegationClientMode.REQUIRED: 2,
}

__all__ = ["EnumDelegationClientMode"]
