# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The sibling premise states the release train can observe."""

from __future__ import annotations

from enum import StrEnum


class EnumSiblingPremiseState(StrEnum):
    """What the release train can conclude about the dev lane's vendor status."""

    READY = "ready"
    PENDING = "pending"
    BEHIND = "behind"
    DEPLOY_NOT_DONE = "deploy_not_done"
    DEADLINE_MISSED = "deadline_missed"
    LANE_UNREADABLE = "lane_unreadable"
    NOT_EXERCISED = "not_exercised"
    ABSENT = "absent"


__all__ = [
    "EnumSiblingPremiseState",
]
