# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Why the pilot manifest check refused a manifest."""

from __future__ import annotations

from enum import StrEnum


class EnumPilotCheckFinding(StrEnum):
    BAR_MISSING = "bar_missing"
    HASH_MISSING = "hash_missing"
    HASH_MISMATCH = "hash_mismatch"
    NOT_FROZEN_BEFORE_START = "not_frozen_before_start"
