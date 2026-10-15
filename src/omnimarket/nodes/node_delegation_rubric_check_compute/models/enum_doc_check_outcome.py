# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Documentation draw outcomes; FAIL covers any claim the checker could not match."""

from enum import StrEnum


class EnumDocCheckOutcome(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
