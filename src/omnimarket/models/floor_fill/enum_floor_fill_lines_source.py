# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The sources a pass reads at S1; one that could not be read stops the pass at S3."""

from __future__ import annotations

from enum import StrEnum


class EnumFloorFillLinesSource(StrEnum):
    """The five sources of a pass."""

    FLOOR = "floor"
    POOL = "pool"
    BRAKE = "brake"
    STOP_SIGNAL = "stop-signal"
    LEDGER = "ledger"
