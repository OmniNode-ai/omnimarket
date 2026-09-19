# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The three states a self-hosted runner can be in (OMN-18768)."""

from __future__ import annotations

from enum import StrEnum


class EnumRunnerStatus(StrEnum):
    """A runner is exactly one of these at an observation.

    ``BUSY`` is a state of its own rather than a flag on ``ONLINE``: folding it
    in makes "the fleet is saturated" indistinguishable from "the fleet is
    idle", which is the question a capacity panel exists to answer.

    There is deliberately no ``UNKNOWN``. An observation that could not read the
    fleet is not published at all — the emitter refuses rather than emitting a
    fleet of unknowns, because an unreadable GitHub API rendered as rows would
    be indistinguishable from a fleet that really is in an unknown state.
    """

    ONLINE = "online"
    BUSY = "busy"
    OFFLINE = "offline"
