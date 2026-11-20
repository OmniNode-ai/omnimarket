# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Where an eval run stands; shared by the eval-results projection and the board that reads it."""

from __future__ import annotations

from enum import StrEnum


class EnumEvalRunState(StrEnum):
    """Where a run stands, from its close and the first tries held for it.

    open     -- no close has arrived.
    closing  -- the close has arrived and a first try it counts is missing or still unscorable
                for an infrastructure reason; no rate is shown.
    closed   -- every first try is in, none infrastructure-unscorable, over the registered tasks.
    void     -- the close named a cause, or the held tries are not the registered ones.
    live     -- a week of live work: no close, one row per model.
    """

    OPEN = "open"
    CLOSING = "closing"
    CLOSED = "closed"
    VOID = "void"
    LIVE = "live"
