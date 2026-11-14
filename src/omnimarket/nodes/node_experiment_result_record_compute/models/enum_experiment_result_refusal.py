# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Refusal codes for the experiment result record compute node."""

from __future__ import annotations

from enum import StrEnum


class EnumExperimentResultRefusal(StrEnum):
    """The cross-field rules the wire model does not check."""

    CORRECT_EXCEEDS_N = "correct_exceeds_n"
    INTERVAL_MISMATCH = "interval_mismatch"
    LATENCY_ORDER = "latency_order"
    ENGINE_NOT_ALLOWED = "engine_not_allowed"


__all__ = ["EnumExperimentResultRefusal"]
