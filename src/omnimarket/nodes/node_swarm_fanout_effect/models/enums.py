# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
from __future__ import annotations

from enum import StrEnum

from omnimarket.models.swarm.enum_execution_status import EnumExecutionStatus

__all__ = ["EnumDispatchMode", "EnumExecutionStatus"]


class EnumDispatchMode(StrEnum):
    DIRECT = "direct"
    QUEUE = "queue"
