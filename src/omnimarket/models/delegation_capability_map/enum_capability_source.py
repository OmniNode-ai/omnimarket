# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Where a capability verdict row came from (OMN-20169)."""

from enum import StrEnum


class EnumCapabilitySource(StrEnum):
    """The source of one verdict row.

    labelled_item: a hand or blind label on a stored eval item (EV.1).
    grid_run: the offline configuration grid over the labelled sets (plan 13.10).
    nightly_rung: the nightly per-rung eval (OMN-19456).
    live_attempt: a live attempt whose class rubric decides (RS.4).
    """

    LABELLED_ITEM = "labelled_item"
    GRID_RUN = "grid_run"
    NIGHTLY_RUNG = "nightly_rung"
    LIVE_ATTEMPT = "live_attempt"
