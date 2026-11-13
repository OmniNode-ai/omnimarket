# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The failure reasons of the dev running-revision read."""

from __future__ import annotations

from enum import StrEnum


class EnumDevRunningRevisionFailureReason(StrEnum):
    """Why the read failed: the source could not be read, or the facts were refused by the compute."""

    SOURCE_UNREADABLE = "source_unreadable"
    FACTS_REFUSED = "facts_refused"


__all__ = ["EnumDevRunningRevisionFailureReason"]
