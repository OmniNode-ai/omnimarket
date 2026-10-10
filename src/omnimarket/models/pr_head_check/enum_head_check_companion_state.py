# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""EnumHeadCheckCompanionState: the change-control companion as seen at a head."""

from __future__ import annotations

from enum import StrEnum


class EnumHeadCheckCompanionState(StrEnum):
    """State of the PR's change-control companion when the head was read.

    - ``NONE``: no companion is bound to the PR.
    - ``OPEN``: a bound companion is open and mergeable.
    - ``CONFLICTING``: a bound companion is open and in merge conflict.
    - ``MERGED``: the bound companion has merged.
    - ``CLOSED``: the bound companion was closed without merging.
    - ``DECLINED``: the companion producer refused to derive one.
    """

    NONE = "none"
    OPEN = "open"
    CONFLICTING = "conflicting"
    MERGED = "merged"
    CLOSED = "closed"
    DECLINED = "declined"


# States in which a companion PR exists and is named.
COMPANION_STATES_WITH_PR: frozenset[EnumHeadCheckCompanionState] = frozenset(
    {
        EnumHeadCheckCompanionState.OPEN,
        EnumHeadCheckCompanionState.CONFLICTING,
        EnumHeadCheckCompanionState.MERGED,
        EnumHeadCheckCompanionState.CLOSED,
    }
)


__all__: list[str] = ["COMPANION_STATES_WITH_PR", "EnumHeadCheckCompanionState"]
