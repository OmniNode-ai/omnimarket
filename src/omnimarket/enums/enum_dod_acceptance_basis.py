# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""What a DoD verdict's acceptance evidence was written from (OMN-20153).

The quality-evidence audit of 2026-09-30 found that 1 of 41 tickets carried a
check written from its own acceptance criteria. Every other verdict rested on
machine-made PR-exists, grep and diff-derived test items, and nothing in the
verdict said so. This enum is the one field that says it: a consumer measuring
quality can now split verdicts by whether the author's own falsifiers were run.
"""

from __future__ import annotations

from enum import StrEnum, unique


@unique
class EnumDodAcceptanceBasis(StrEnum):
    """Which evidence the verdict's acceptance question was answered from."""

    #: At least one accepted criterion's falsifier was turned into a runnable
    #: check and executed. Its outcome is inside the verdict: a failing or
    #: zero-test falsifier makes the verdict FAILED.
    FALSIFIER_CHECKS = "falsifier_checks"

    #: The ticket declared accepted falsifiers, but none names a test selector a
    #: machine can run (prose, a lab query, a readback). Reported so the gap is
    #: visible instead of reading as "no criteria".
    FALSIFIERS_UNRUNNABLE = "falsifiers_unrunnable"

    #: The contract carries no accepted falsifier at all. A verdict that would
    #: read VERIFIED on PR-exists or grep evidence alone is not VERIFIED.
    NO_ACCEPTANCE_CHECKS = "no_acceptance_checks"


__all__ = ["EnumDodAcceptanceBasis"]
