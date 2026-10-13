# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Status of one branch-protection audit line."""

from enum import StrEnum


class EnumBranchProtectionCheckStatus(StrEnum):
    """PASS and FAIL judge; WARN, NOTE and SKIP never fail the audit."""

    PASS = "PASS"
    FAIL = "FAIL"
    WARN = "WARN"
    NOTE = "NOTE"
    SKIP = "SKIP"


__all__ = ["EnumBranchProtectionCheckStatus"]
