# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Why an activation reached no installation fact (OMN-21112, TA.10)."""

from __future__ import annotations

from enum import StrEnum, unique


@unique
class EnumContextInstallUncertainReason(StrEnum):
    """The closed reasons an ``-install-uncertain.v1`` event carries.

    ``native_compaction`` and ``not_selected`` are the two ``not_installed``
    reasons; ``not_installed`` rides here rather than on a topic of its own.
    """

    NATIVE_COMPACTION = "native_compaction"
    NOT_SELECTED = "not_selected"
    CAPTURE_GAP = "capture_gap"
    UNPARSEABLE_RECORD = "unparseable_record"


__all__ = ["EnumContextInstallUncertainReason"]
