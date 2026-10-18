# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The deterministic verdict on one saved rescue patch."""

from __future__ import annotations

from enum import StrEnum


class EnumRescuePatchVerdict(StrEnum):
    """A discard is certain from the patch and the base; anything else needs a read."""

    DISCARD_EMPTY = "discard-empty"
    DISCARD_SUPERSEDED = "discard-superseded"
    DISCARD_JUNK = "discard-junk"
    DISCARD_FORMATTING = "discard-formatting"
    NEEDS_READ = "needs-read"

    @property
    def is_discard(self) -> bool:
        return self.value.startswith("discard-")
