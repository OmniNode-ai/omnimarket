# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What happened to one key this tick."""

from __future__ import annotations

from enum import StrEnum


class EnumFloorFillLinesStartKind(StrEnum):
    """The process effect started a lane, answered failed, or the ticket mint was refused."""

    STARTED = "started"
    FAILED = "failed"
    MINT_REFUSED = "mint-refused"
