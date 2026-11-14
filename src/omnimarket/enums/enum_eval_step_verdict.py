# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The step verdict of a scored try and who gave it (OMN-24170)."""

from __future__ import annotations

from enum import StrEnum


class EnumEvalStepVerdict(StrEnum):
    """Accept or reject the step, or none when the scorer gave no step verdict."""

    ACCEPT = "accept"
    REJECT = "reject"
    NONE = "none"


class EnumEvalStepVerdictSource(StrEnum):
    """Who gave the step verdict."""

    DETERMINISTIC = "deterministic"
    REVIEWER_MODEL = "reviewer_model"


__all__ = ["EnumEvalStepVerdict", "EnumEvalStepVerdictSource"]
