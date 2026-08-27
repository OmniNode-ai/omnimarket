# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What the projection actually knows about upstream production (OMN-16777)."""

from __future__ import annotations

from enum import StrEnum


class EnumUpstreamEvidence(StrEnum):
    """Why a zero-in row was called STARVED or IDLE.

    Recorded as its own column rather than collapsed into the verdict, so a row
    always says how much it knows. "IDLE because the topic was provably silent"
    and "IDLE because we have no way to see this topic's producers" are the same
    verdict reached for very different reasons, and an operator triaging a dead
    chain needs to be able to tell them apart.
    """

    #: The platform published to this topic during the window.
    PRODUCED = "PRODUCED"

    #: The platform published to this topic zero times, and it does publish
    #: there — the silence is observed, not assumed.
    SILENT = "SILENT"

    #: No evidence either way. Nothing in this runtime publishes to the topic,
    #: so an external producer (an MSK ingress leg, a client gateway) is
    #: invisible on this rail. The row reports IDLE and says so, rather than
    #: guessing STARVED — a guess here is an alert that fires on every quiet
    #: externally-fed topic in the platform.
    NONE = "NONE"


__all__ = ["EnumUpstreamEvidence"]
