# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Cross-run delegation lineage, as one shared vocabulary.

The request carries lineage in ``metadata`` under the keys declared here.
The terminal projection decodes those values before a producer emits them.
A malformed value is refused by name and never guessed into lineage.
"""

from __future__ import annotations

from uuid import UUID

#: The request ``metadata`` keys naming the parent run and this attempt.
DELEGATION_PARENT_CORRELATION_METADATA_KEY = "parent_correlation_id"
DELEGATION_ATTEMPT_KIND_METADATA_KEY = "attempt_kind"
DELEGATION_PARENT_FAILURE_CAUSE_METADATA_KEY = "parent_failure_cause"

#: The declared kinds of delegation attempt.
ATTEMPT_KINDS = ("first", "escalation", "engine_fallback", "retry", "locus_fallback")


def attempt_kind_refusal(value: object) -> str | None:
    """Return why ``value`` is not an attempt kind, or None when it is one."""
    if not isinstance(value, str):
        return f"attempt_kind is not a string: {type(value).__name__}"
    if value not in ATTEMPT_KINDS:
        return f"attempt_kind {value!r} is not one of {ATTEMPT_KINDS!r}"
    return None


def parent_correlation_refusal(value: object) -> str | None:
    """Return why ``value`` is not a canonical parent UUID, or None."""
    if not isinstance(value, str):
        return f"parent_correlation_id is not a string: {type(value).__name__}"
    try:
        canonical = str(UUID(value))
    except ValueError:
        return f"parent_correlation_id {value!r} is not a canonical lowercase UUID"
    if value != canonical:
        return f"parent_correlation_id {value!r} is not a canonical lowercase UUID"
    return None


__all__ = [
    "ATTEMPT_KINDS",
    "DELEGATION_ATTEMPT_KIND_METADATA_KEY",
    "DELEGATION_PARENT_CORRELATION_METADATA_KEY",
    "DELEGATION_PARENT_FAILURE_CAUSE_METADATA_KEY",
    "attempt_kind_refusal",
    "parent_correlation_refusal",
]
