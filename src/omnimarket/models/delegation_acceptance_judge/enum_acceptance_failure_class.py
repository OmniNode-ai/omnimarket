# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The closed failure-class vocabulary a judge may name."""

from __future__ import annotations

from enum import StrEnum


class EnumAcceptanceFailureClass(StrEnum):
    """The closed failure-class vocabulary a judge may name."""

    NONE = "none"
    FABRICATION = "fabrication"
    WRONG_ANSWER = "wrong_answer"
    CONSTRAINT_VIOLATION = "constraint_violation"
    INCOMPLETE = "incomplete"
    OFF_TASK = "off_task"
    BROKEN_CODE = "broken_code"
    MALFORMED_OUTPUT = "malformed_output"
    REASONING_LEAK = "reasoning_leak"
    FALSE_FINDINGS = "false_findings"
    EMPTY_OR_REFUSAL = "empty_or_refusal"
    OTHER = "other"
