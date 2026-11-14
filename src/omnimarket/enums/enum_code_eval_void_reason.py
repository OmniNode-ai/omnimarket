# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Why a code eval run closed void (OMN-24173)."""

from __future__ import annotations

from enum import StrEnum


class EnumCodeEvalVoidReason(StrEnum):
    """The cause named on the closed event of a run that draws no point.

    registration_mismatch                   -- a try's endpoint reported a model revision other
                                               than the one the run registered for that model.
    infrastructure_unscorable_after_retries -- a try was still infrastructure-unscorable after
                                               its last retry.
    infrastructure_unscorable_first_tries   -- more than the overlay's share of first tries were
                                               infrastructure-unscorable before retries.
    """

    REGISTRATION_MISMATCH = "registration_mismatch"
    INFRASTRUCTURE_UNSCORABLE_AFTER_RETRIES = "infrastructure_unscorable_after_retries"
    INFRASTRUCTURE_UNSCORABLE_FIRST_TRIES = "infrastructure_unscorable_first_tries"


__all__ = ["EnumCodeEvalVoidReason"]
