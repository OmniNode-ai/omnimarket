# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Values for EnumDelegationEvalGateOutcome."""

from enum import StrEnum


class EnumDelegationEvalGateOutcome(StrEnum):
    ACCEPTED = "accepted"
    REFUSED = "refused"
    UNDETERMINED = "undetermined"
