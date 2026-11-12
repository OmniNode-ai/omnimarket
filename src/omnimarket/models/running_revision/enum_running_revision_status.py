# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overall status of a running-revision reading."""

from enum import StrEnum


class EnumRunningRevisionStatus(StrEnum):
    """Current, lagging (behind but not past the threshold), behind, or unknown."""

    CURRENT = "current"
    LAGGING = "lagging"
    BEHIND = "behind"
    UNKNOWN = "unknown"
