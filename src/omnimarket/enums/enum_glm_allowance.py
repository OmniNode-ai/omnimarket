# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Verdicts of the GLM Coding Plan allowance check (OMN-20287)."""

from __future__ import annotations

from enum import StrEnum, unique


@unique
class EnumGlmAllowanceVerdict(StrEnum):
    """What the allowance says about the GLM rungs of one chain."""

    PREFER_GLM = "prefer_glm"
    """Allowance is unused and the class is approved: GLM moves ahead of costlier rungs."""

    ALLOW_GLM = "allow_glm"
    """GLM may run where the chain already puts it."""

    SKIP_GLM = "skip_glm"
    """Every GLM rung is dropped; work goes to the next rung."""

    NO_GLM_RUNG = "no_glm_rung"
    """The chain has no GLM rung; nothing to decide."""


@unique
class EnumGlmSkipReason(StrEnum):
    """Why a GLM rung was dropped from a chain."""

    CLASS_NOT_APPROVED = "class_not_approved"
    BUDGET_BELOW_CLASS_MINIMUM = "budget_below_class_minimum"
    PROVIDER_REFUSAL_COOLDOWN = "provider_refusal_cooldown"
    WINDOW_NEAR_CAP = "window_near_cap"
    WEEK_NEAR_CAP = "week_near_cap"


@unique
class EnumGlmRefusalScope(StrEnum):
    """Which cap a provider refusal belongs to."""

    WINDOW = "window"
    WEEK = "week"
    RATE = "rate"
    """A rate or concurrency refusal, or a refusal whose code the policy does not map."""


@unique
class EnumGlmRunOutcome(StrEnum):
    """How a harness run ended, as its receipt records it."""

    DONE = "done"
    RUN_FAILED = "run-failed"
    BUDGET_REFUSED = "budget-refused"
    UNAVAILABLE = "unavailable"
