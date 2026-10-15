# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Why an experiment results record was refused."""

from __future__ import annotations

from enum import StrEnum


class EnumExperimentRefusal(StrEnum):
    """Closed set. The first group is AC1, then AC2, AC3 and AC4 of the experiment program."""

    MISSING_RECORD = "missing_record"
    DUPLICATE_EXPERIMENT = "duplicate_experiment"
    MISSING_HYPOTHESIS = "missing_hypothesis"
    MISSING_METRIC = "missing_metric"
    MISSING_BASELINE = "missing_baseline"
    MISSING_VERDICT = "missing_verdict"
    VOID_WITHOUT_CAUSE = "void_without_cause"
    TOO_FEW_ARMS = "too_few_arms"
    DUPLICATE_ARM = "duplicate_arm"
    EMPTY_ARM = "empty_arm"
    MISSING_ARM_VALUE = "missing_arm_value"
    UNTRACEABLE_RUN = "untraceable_run"
    DUPLICATE_RUN = "duplicate_run"
    ROUTING_CHANGE = "routing_change"
    SUPPORTED_WITHOUT_FOLLOW_ON = "supported_without_follow_on"
    RETIRED_MODEL = "retired_model"
