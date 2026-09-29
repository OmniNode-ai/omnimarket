# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Values for EnumGateVerdict."""

from enum import StrEnum


class EnumGateVerdict(StrEnum):
    ACCEPTED = "accepted"
    REFUSED = "refused"
    UNDETERMINED = "undetermined"
