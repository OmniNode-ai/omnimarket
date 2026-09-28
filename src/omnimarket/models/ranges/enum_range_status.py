# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Whether a range check's acceptance line has been declared."""

from __future__ import annotations

from enum import StrEnum


class EnumRangeStatus(StrEnum):
    """A range half is NOT SET until all four parts of its method are declared.

    A NOT SET range judges nothing yet; the check keeps blocking on its gate
    half, and nothing about NOT SET makes the check advisory (section 2b, rule 7).
    """

    DECLARED = "declared"
    NOT_SET = "not_set"
