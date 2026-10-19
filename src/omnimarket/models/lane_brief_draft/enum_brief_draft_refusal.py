# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Why a drafted brief was refused before the checker saw it."""

from __future__ import annotations

from enum import StrEnum


class EnumBriefDraftRefusal(StrEnum):
    """Closed set of defects in the model's slots."""

    MISSING_SLOT = "missing_slot"
    DUPLICATE_SLOT = "duplicate_slot"
    EMPTY_SLOT = "empty_slot"
    MULTILINE_TITLE = "multiline_title"
    SHELL_PART_IN_DRAFT = "shell_part_in_draft"
    UNGROUNDED_IDENTIFIER = "ungrounded_identifier"
