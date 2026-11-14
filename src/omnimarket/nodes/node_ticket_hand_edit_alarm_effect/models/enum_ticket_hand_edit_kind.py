# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The two typed errors this node raises."""

from __future__ import annotations

from enum import StrEnum


class EnumTicketHandEditKind(StrEnum):
    """Both kinds are findings: each raises one error event and one alarm."""

    TICKET_HAND_EDIT = "ticket_hand_edit"
    DETECTOR_UNREADABLE = "detector_unreadable"
