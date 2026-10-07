# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What the landing lane is asked to do with the PR; the handoff MSG's ``needs=`` cell."""

from __future__ import annotations

from enum import StrEnum


class EnumPrHandoffNeeds(StrEnum):
    """What the landing lane is asked to do with the PR; the handoff MSG's ``needs=`` cell."""

    LAND = "land"
    """Ready to land at its head."""

    TRAIN = "train"
    """Runtime-affecting; ready for the runtime train at its head."""

    COMPANION = "companion"
    """Waiting on its change-control companion at its head; a draft is accepted."""


__all__: list[str] = ["EnumPrHandoffNeeds"]
