# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What a missing, cancelled or incomplete run, or an unscored sample, counts as."""

from __future__ import annotations

from enum import StrEnum


class EnumIncompleteRunTreatment(StrEnum):
    """The incomplete-run treatment of a range line.

    One member, on purpose. Section 2b rules that every missing, cancelled or
    incomplete run counts as a failure and is never excluded. The field is
    still required on every line, so a line that did not state its treatment
    is refused rather than defaulted.
    """

    COUNT_AS_FAILURE = "count_as_failure"
