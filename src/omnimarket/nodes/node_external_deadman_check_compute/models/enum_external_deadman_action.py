# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What the workflow does with the GitHub issue (OMN-20808)."""

from __future__ import annotations

from enum import StrEnum


class EnumExternalDeadmanAction(StrEnum):
    """The one issue the external dead-man keeps: open it, refresh it, or leave it."""

    #: Nothing is wrong. An issue already open stays open for the operator to close.
    NONE = "none"
    OPEN_ISSUE = "open_issue"
    #: An issue is already open: its body is refreshed, never a second one opened.
    UPDATE_ISSUE = "update_issue"
