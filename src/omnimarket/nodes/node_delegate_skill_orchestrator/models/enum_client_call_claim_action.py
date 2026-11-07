# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What a claim attempt tells its caller to do next (plan TB.3, OMN-21098)."""

from __future__ import annotations

from enum import StrEnum


class EnumClientCallClaimAction(StrEnum):
    """The verdict of one claim attempt on a client-call key."""

    WON = "won"
    CONFLICT = "conflict"
    REPUBLISH_TERMINAL = "republish_terminal"
    WAIT_LIVE_OWNER = "wait_live_owner"
    TAKEOVER = "takeover"


class EnumClientCallResumeStep(StrEnum):
    """Where a takeover resumes, read from the row's recorded state.

    ``AWAIT_TERMINAL`` is the only resume for a row that recorded its dispatch
    intent: a recorded intent is never dispatched again.
    """

    ADMIT = "admit"
    PUBLISH_ADMISSION = "publish_admission"
    AWAIT_TERMINAL = "await_terminal"
