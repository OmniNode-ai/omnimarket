# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The four-state flow verdict, plus the fifth state that is not a verdict.

OMN-16777 / epic OMN-16776.  Every liveness signal the platform had before this
was binary — up or down — and every one of the four failures on 2026-08-23 was a
consumer that was up.  A flow row must express FOUR states, and a design that
cannot separate STALLED/STARVED from IDLE has not solved the problem.
"""

from __future__ import annotations

from enum import StrEnum


class EnumConsumerFlowState(StrEnum):
    """Verdict for one (consumer_group, topic) window.

    Derived HERE, in the projection — never stamped on the producing event.  The
    heartbeat carries raw counters only, because a producer that grades its own
    health is a producer that can lie about it.
    """

    #: in > 0, out > 0 — messages went through the seam.
    FLOWING = "FLOWING"

    #: in > 0, out == 0 — the consumer took everything and produced nothing.
    #: This is OMN-16755: Stable, LAG 0, offset 15,750, output topic at 0.
    STALLED = "STALLED"

    #: in == 0, out == 0, and something WAS producing upstream in this window.
    #: Messages existed and this consumer did not take them.
    STARVED = "STARVED"

    #: in == 0, out == 0, and nothing was producing upstream either. Quiet, and
    #: correctly so. Reporting this as a problem is the alert storm that makes a
    #: monitor worthless (AC4).
    IDLE = "IDLE"

    #: The window was never observed — a heartbeat went missing. NOT a verdict
    #: and NOT zero traffic. Materializing a dropped window as "no messages"
    #: reintroduces the exact defect this ticket exists to close (AC5).
    UNKNOWN = "UNKNOWN"


__all__ = ["EnumConsumerFlowState"]
