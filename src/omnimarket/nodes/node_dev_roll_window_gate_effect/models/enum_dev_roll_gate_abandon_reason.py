# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Why the gate stopped waiting for a released roll."""

from enum import StrEnum


class EnumDevRollGateAbandonReason(StrEnum):
    """One typed reason per abandoned roll."""

    COMPLETION_DEADLINE_PASSED = "completion_deadline_passed"
