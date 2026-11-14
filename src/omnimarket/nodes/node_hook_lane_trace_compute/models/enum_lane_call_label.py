# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
from __future__ import annotations

from enum import StrEnum


class EnumLaneCallLabel(StrEnum):
    """The label printed for one call of a lane: the four typed outcomes, or no stored output to label."""

    OK = "ok"
    FAILED = "failed"
    REFUSED = "refused"
    TIMEOUT = "timeout"
    NO_OUTPUT = "no-output"
