# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What a landing observation reports about one PR."""

from __future__ import annotations

from enum import StrEnum


class EnumPrLandingObservationKind(StrEnum):
    """The closed set of facts the landing workflow reacts to.

    A value outside this set is refused at the model boundary, never mapped to
    a default: an observation the reducer cannot classify must not move a PR.
    """

    PUSHED = "pushed"
    READY_FOR_REVIEW = "ready_for_review"
    CONVERTED_TO_DRAFT = "converted_to_draft"
    TITLE_EDITED = "title_edited"
    HOLD_APPLIED = "hold_applied"
    HOLD_LIFTED = "hold_lifted"
    REOPENED = "reopened"
    COMPANION_OUTCOME = "companion_outcome"
    COMPANION_MERGED = "companion_merged"
    HEAD_CHECKS = "head_checks"
    ARMED_CONFIRMED = "armed_confirmed"
    DISARMED = "disarmed"
    MERGED = "merged"
    CLOSED = "closed"
    BOUND_EXPIRED = "bound_expired"


__all__: list[str] = ["EnumPrLandingObservationKind"]
