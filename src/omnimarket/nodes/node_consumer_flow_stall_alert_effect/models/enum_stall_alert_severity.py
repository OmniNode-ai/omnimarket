# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Degradation severity, kept separate from the outcome (OMN-16778).

Doctrine gate 8 requires FAIL and WARN to be distinguishable rather than
collapsed into "something is wrong".  The outcome says *what was concluded*;
the severity says *how bad it is*.  Keeping them apart is what lets the
terminal event carry a WARN that is visible to a dashboard without pushing it
into a Slack channel.
"""

from __future__ import annotations

from enum import StrEnum


class EnumStallAlertSeverity(StrEnum):
    """Severity of one stall evaluation."""

    #: Nothing degraded.
    NONE = "NONE"

    #: A single missed window, or a run that has not yet been confirmed.
    WARN = "WARN"

    #: A confirmed stall or starvation past the declared threshold.
    FAIL = "FAIL"


__all__ = ["EnumStallAlertSeverity"]
