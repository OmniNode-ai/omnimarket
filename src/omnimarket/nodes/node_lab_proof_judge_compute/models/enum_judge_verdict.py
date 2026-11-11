# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""EnumJudgeVerdict: the verdict of the lab proof judge."""

from __future__ import annotations

from enum import StrEnum


class EnumJudgeVerdict(StrEnum):
    """The verdict of the lab proof judge."""

    PASS = "PASS"
    FAIL = "FAIL"
    NOT_APPLICABLE = "NOT_APPLICABLE"
