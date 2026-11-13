# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The revision of a scheduled source: the latest cron slot at or before now.

A list of a scheduled workflow's runs can change only when a slot passes and its run appears (or finishes, or a manual dispatch adds
one), so a read taken in a slot is current until the next slot passes and its newest run is at or after the slot. The caller decides
whether a cached read has seen the slot's run; this module names the slot.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from omnimarket.models.floor_17a.model_mvp_launch import ModelCron

SLOT_STAMP = "%Y%m%dT%H%M%SZ"


def schedule_slot(crons: Sequence[str], now: datetime) -> datetime | None:
    """The latest of the crons' slots at or before ``now`` (UTC), or ``None`` when no cron yields one."""
    slots = [
        slot
        for text in crons
        if (slot := ModelCron.parse(text).latest_at_or_before(now)) is not None
    ]
    return max(slots) if slots else None
