# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What one evaluation of a consumer's window history concluded (OMN-16778).

Five outcomes, because the four the platform could previously express were not
enough to keep an alert channel readable.  ``FAIL`` and ``WARN`` are different
facts and the ticket forbids conflating them: a channel that receives both at
the same volume gets muted, and a muted channel is worth exactly as much as no
channel (the OMN-14440 precedent).
"""

from __future__ import annotations

from enum import StrEnum


class EnumStallAlertOutcome(StrEnum):
    """The verdict of one stall evaluation."""

    #: The trailing alerting run reached the declared confirm threshold. This
    #: is the only outcome that publishes a Slack alert by default.
    FAIL_CONFIRMED_STALL = "FAIL_CONFIRMED_STALL"

    #: An alerting run is under way but has not yet reached confirm_windows.
    #: Deliberately silent: firing here is the flap that made the .201 host
    #: reporter's own alerts unreadable (OMN-16789).
    PENDING_CONFIRMATION = "PENDING_CONFIRMATION"

    #: The newest window was never observed. Not healthy and not stalled --
    #: a missed heartbeat is the absence of an observation (OMN-16777 AC5).
    WARN_MISSED_WINDOW = "WARN_MISSED_WINDOW"

    #: An earlier confirmed stall has stopped, but not for clear_windows yet.
    #: Silent, and deliberately not "recovered": a stall that blips healthy for
    #: one window has not recovered.
    RECOVERING = "RECOVERING"

    #: Nothing to say. Either flowing, or genuinely idle on a quiet topic --
    #: which is the false-positive half of this ticket (AC3), not a defect.
    NO_ALERT = "NO_ALERT"


__all__ = ["EnumStallAlertOutcome"]
