# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Whether the result of an operation may be relied on."""

from __future__ import annotations

from enum import StrEnum


class EnumAcceptanceRunStatus(StrEnum):
    """Whether the result of an operation may be relied on."""

    PASSED = "passed"
    FAILED = "failed"
