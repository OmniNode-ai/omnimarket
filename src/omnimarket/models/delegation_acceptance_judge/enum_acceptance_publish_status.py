# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Whether the judged events reached the emit daemon."""

from __future__ import annotations

from enum import StrEnum


class EnumAcceptancePublishStatus(StrEnum):
    """The terminal outcome vocabulary the runtime reads: completed or failed."""

    COMPLETED = "completed"
    FAILED = "failed"
