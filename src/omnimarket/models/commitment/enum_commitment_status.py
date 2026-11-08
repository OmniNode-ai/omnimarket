# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Where a commitment stands."""

from __future__ import annotations

from enum import StrEnum


class EnumCommitmentStatus(StrEnum):
    """Open until its check passes, verified while it passes, regressed when it failed after passing.

    Withdrawn is final: the operator no longer wants the ask, so it is no work and its check is never run."""

    OPEN = "open"
    VERIFIED = "verified"
    REGRESSED = "regressed"
    WITHDRAWN = "withdrawn"
