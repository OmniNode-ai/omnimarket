# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Where a pilot stands against its frozen bar. There is no extended state."""

from __future__ import annotations

from enum import StrEnum


class EnumPilotStatus(StrEnum):
    REFUSED = "refused"
    READY = "ready"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
