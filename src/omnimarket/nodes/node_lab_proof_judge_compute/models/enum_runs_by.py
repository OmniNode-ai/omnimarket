# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""EnumRunsBy: how a node runs outside the runtime."""

from __future__ import annotations

from enum import StrEnum


class EnumRunsBy(StrEnum):
    """How a node runs outside the runtime."""

    COMMAND = "command"
    SCHEDULE = "schedule"
    HOST_UNIT = "host_unit"
