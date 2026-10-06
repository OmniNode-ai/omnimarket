# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The decision compute's verdict for one evaluation."""

from __future__ import annotations

from enum import StrEnum


class EnumPrHandoffVerdict(StrEnum):
    """The decision compute's verdict for one evaluation."""

    READY = "ready"
    """Hand it off now: the rows are in the decision."""

    WAIT = "wait"
    """Not yet: the live state may still become ready (wait_reason says why)."""

    REFUSE = "refuse"
    """Never at this request: error_code says why."""


__all__: list[str] = ["EnumPrHandoffVerdict"]
