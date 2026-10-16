# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Answer oracle outcomes, distinct from route evidence outcomes."""

from enum import StrEnum


class EnumAnswerCheckOutcome(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
