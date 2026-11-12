# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Why a repository's running revision could not be read."""

from enum import StrEnum


class EnumRunningRevisionUnknownReason(StrEnum):
    """Set exactly when a repository reading's status is UNKNOWN."""

    HISTORY_MISSING = "history_missing"
    NO_RUNNING_BUILD = "no_running_build"
    REVISION_MISSING_FROM_MANIFEST = "revision_missing_from_manifest"
    RUNNING_REVISION_NOT_IN_HISTORY = "running_revision_not_in_history"
