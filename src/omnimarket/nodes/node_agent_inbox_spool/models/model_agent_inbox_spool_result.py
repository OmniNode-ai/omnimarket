# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What one read spooled, skipped and refused, plus the group actually used."""

from ._base import ModelFrozen


class ModelAgentInboxSpoolResult(ModelFrozen):
    consumer_group: str
    groups_used: tuple[str, ...]
    events_read: int
    spooled: tuple[str, ...]
    duplicates: int
    unaddressed: int
    malformed: int
    unread_lanes: tuple[str, ...]
    read_ok: bool
    error: str
