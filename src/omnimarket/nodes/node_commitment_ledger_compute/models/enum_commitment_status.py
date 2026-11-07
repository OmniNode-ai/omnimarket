# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Where a commitment stands."""

from __future__ import annotations

from enum import StrEnum


class EnumCommitmentStatus(StrEnum):
    """A commitment is open until its check passes, verified while it passes, regressed when it failed after passing."""

    OPEN = "open"
    VERIFIED = "verified"
    REGRESSED = "regressed"
