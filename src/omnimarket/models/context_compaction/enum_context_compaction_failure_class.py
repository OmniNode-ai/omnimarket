# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Why a compaction fell back to native compaction (OMN-21113, TA.11)."""

from __future__ import annotations

from enum import StrEnum


class EnumContextCompactionFailureClass(StrEnum):
    """The closed failure classes a ``-fallback.v1`` event carries.

    The four source classes are the ones the compaction transaction names; the
    three build classes say whether the package path answered at all.
    """

    SOURCE_NOT_CAUGHT_UP = "source_not_caught_up"
    SOURCE_INCOMPLETE = "source_incomplete"
    SOURCE_DISABLED = "source_disabled"
    NO_SOURCE_TIMESTAMP = "no_source_timestamp"
    PACK_BUILD_FAILED = "pack_build_failed"
    PACK_BUILDER_UNAVAILABLE = "pack_builder_unavailable"
    BUILD_ERROR = "build_error"


__all__ = ["EnumContextCompactionFailureClass"]
