# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""How the P8a transcript-form rule classifies a record (OMN-21129, plan task TC.6)."""

from __future__ import annotations

from enum import StrEnum


class EnumTranscriptPromptClass(StrEnum):
    """Only OPERATOR_TYPED is an operator prompt; every other user class is synthetic."""

    #: A user record carrying `promptSource`, not meta, not a compact summary, no tool result.
    OPERATOR_TYPED = "operator_typed"
    #: `isMeta: true`, such as the `<local-command-caveat>` record.
    SYNTHETIC_META = "synthetic_meta"
    #: `isCompactSummary: true`, the native compaction summary.
    SYNTHETIC_COMPACT_SUMMARY = "synthetic_compact_summary"
    #: The message content holds a `tool_result` block.
    SYNTHETIC_TOOL_RESULT = "synthetic_tool_result"
    #: No `promptSource`: installed replacement records, slash-command records and
    #: records written by builds that predate the field.
    SYNTHETIC_NO_PROMPT_SOURCE = "synthetic_no_prompt_source"
    #: An assistant or system record, or a user record with no text.
    NOT_A_PROMPT = "not_a_prompt"
