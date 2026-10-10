# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""EnumCheckRunStatus: the check-runs API ``status`` of one check-run copy."""

from __future__ import annotations

from enum import StrEnum


class EnumCheckRunStatus(StrEnum):
    """A check-run's ``status``; only ``COMPLETED`` carries a conclusion."""

    QUEUED = "queued"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    WAITING = "waiting"
    REQUESTED = "requested"
    PENDING = "pending"


__all__: list[str] = ["EnumCheckRunStatus"]
