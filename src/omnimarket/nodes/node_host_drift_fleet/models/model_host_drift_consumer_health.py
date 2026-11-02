# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Consumer health carried by heartbeat extensions."""

from ._base import ModelFrozen


class ModelHostDriftConsumerHealth(ModelFrozen):
    group_id: str
    ok: bool
    events_read: int
    malformed: int
    replayed: bool
    error: str
