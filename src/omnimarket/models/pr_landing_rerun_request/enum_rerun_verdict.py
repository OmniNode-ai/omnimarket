# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The verdicts a rerun request is answered with (OMN-23830)."""

from __future__ import annotations

from enum import StrEnum


class EnumRerunVerdict(StrEnum):
    """RERUN and DUPLICATE are answered on the completed topic; the other three on the failed topic."""

    RERUN = "rerun"
    DUPLICATE = "duplicate"
    LIMIT_REACHED = "limit_reached"
    HEAD_MOVED = "head_moved"
    JOB_NOT_FAILED = "job_not_failed"

    @property
    def is_refusal(self) -> bool:
        return self in (
            EnumRerunVerdict.LIMIT_REACHED,
            EnumRerunVerdict.HEAD_MOVED,
            EnumRerunVerdict.JOB_NOT_FAILED,
        )


__all__: list[str] = ["EnumRerunVerdict"]
