# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The projection contract rules this node decides."""

from enum import StrEnum


class EnumProjectionContractRule(StrEnum):
    ACCESS = "access"
    DLQ = "dlq"
    CURSOR = "cursor"
    TYPED_ENTRY = "typed-entry"
