# SPDX-License-Identifier: MIT
"""Historical grouping vocabulary retained for queued PR-fix commands."""

from enum import StrEnum


class EnumOccBatchMode(StrEnum):
    """OCC companion grouping (OMN-16336).

    WINDOW is the default everywhere: one companion per product repository per
    batch window, carrying the evidence of every member PR whatever ticket each
    one cites (operator ruling 2026-09-27T10:32:50Z, "one OCC ticket instead of
    30"). A window lasts as long as its companion is open; once it merges, the
    next member opens the next window on the same deterministic branch.

    TICKET groups only PRs whose title cites the same single ticket. OFF (one
    companion per product PR) is only ever asked for explicitly, by the re-mint
    of a legacy per-PR companion or by a repository that turned batching off,
    which queue health reports.
    """

    OFF = "off"
    TICKET = "ticket"
    WINDOW = "window"


__all__ = ["EnumOccBatchMode"]
