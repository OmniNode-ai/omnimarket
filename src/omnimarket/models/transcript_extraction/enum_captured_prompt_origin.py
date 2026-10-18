# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Origin of a captured prompt record, by the P8a rule (OMN-21139, plan task TE.2)."""

from enum import StrEnum


class EnumCapturedPromptOrigin(StrEnum):
    """Only OPERATOR_TYPED is admissible; every other origin is synthetic text."""

    OPERATOR_TYPED = "operator_typed"
    COMPACT_SUMMARY = "compact_summary"
    TOOL_RESULT = "tool_result"
    HARNESS_INJECTED = "harness_injected"
