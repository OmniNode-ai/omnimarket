# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The result a pr-head lab-proof receipt carries (OMN-19566)."""

from __future__ import annotations

from enum import StrEnum


class EnumLabProofReceiptResult(StrEnum):
    """PASS or FAIL, exactly as the receipt derived it from its checks.

    A FAIL is recorded as faithfully as a PASS: the proof bar counts one
    negative-control FAIL per profile, and a record that exists only on success
    cannot tell a failed proof from one nobody ran.
    """

    PASS = "PASS"
    FAIL = "FAIL"


__all__ = ["EnumLabProofReceiptResult"]
