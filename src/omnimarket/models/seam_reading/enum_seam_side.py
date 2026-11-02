# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Which side of a seam an emitter reports for."""

from __future__ import annotations

from enum import StrEnum


class EnumSeamSide(StrEnum):
    SENDER = "sender"
    RECEIVER = "receiver"
