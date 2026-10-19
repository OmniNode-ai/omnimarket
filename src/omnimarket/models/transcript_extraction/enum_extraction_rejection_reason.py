# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Why the verifier dropped a proposed extraction (OMN-21139, plan task TE.2)."""

from enum import StrEnum


class EnumExtractionRejectionReason(StrEnum):
    """Each dropped proposal is counted under exactly one reason, plan section 5.1.

    PARSE_FAILURE: the proposal is not a valid proposed-event object.
    UNKNOWN_SOURCE: no captured prompt in the request has the cited prompt_id and event_id.
    SYNTHETIC_SOURCE: the cited record is not operator-typed (a compact summary, a tool
    result, harness-injected text).
    WRONG_OFFSETS: the quote is in the cited prompt, but not at the cited offsets.
    ABSENT: the quote is not in the cited prompt.
    """

    PARSE_FAILURE = "parse_failure"
    UNKNOWN_SOURCE = "unknown_source"
    SYNTHETIC_SOURCE = "synthetic_source"
    WRONG_OFFSETS = "wrong_offsets"
    ABSENT = "absent"
