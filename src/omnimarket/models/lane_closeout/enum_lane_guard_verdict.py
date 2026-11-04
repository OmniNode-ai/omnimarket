# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The operations guard's verdict on one tree."""

from __future__ import annotations

from enum import StrEnum


class EnumLaneGuardVerdict(StrEnum):
    """``ok`` nothing would be lost, ``wip`` its commits went to a WIP branch, ``keep`` it must stay."""

    OK = "ok"
    WIP = "wip"
    KEEP = "keep"
