# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OR.1 difference reason codes for the OCC retirement pilot (OMN-20072)."""

from enum import StrEnum


class EnumOccVerdictDifferenceReason(StrEnum):
    """Published reason codes, including the case-sensitive PR binding code."""

    READBACK_ONLY = "readback_only"
    INCOMPLETE_CRITERION_COVERAGE = "incomplete_criterion_coverage"
    CIRCULAR_CONTRACT = "circular_contract"
    FINAL_NEWLINE = "final_newline"
    PR_NUMBER_ONLY_BINDING = "PR_number_only_binding"
    FOREIGN_POLICY_OUTSIDE_DECLARED_MANIFEST = (
        "foreign_policy_outside_declared_manifest"
    )
    OLD_BEHAVIORAL_REFUSAL = "old_behavioral_refusal"
    UNCLASSIFIED = "unclassified"
    ACCEPTED_NEGATIVE_CONTROL = "accepted_negative_control"


__all__ = ["EnumOccVerdictDifferenceReason"]
