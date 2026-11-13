# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The four verdicts one merge can receive."""

from __future__ import annotations

from enum import StrEnum


class EnumMergeOverrideVerdict(StrEnum):
    """Landing and override_merge are quiet; the other two are findings."""

    LANDING = "landing"
    OVERRIDE_MERGE = "override_merge"
    MERGE_WITHOUT_OVERRIDE = "merge_without_override"
    DETECTOR_UNREADABLE = "detector_unreadable"

    @property
    def is_finding(self) -> bool:
        return self in (
            EnumMergeOverrideVerdict.MERGE_WITHOUT_OVERRIDE,
            EnumMergeOverrideVerdict.DETECTOR_UNREADABLE,
        )
