# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The landing controller's red class of one head (the ci-watch classes)."""

from __future__ import annotations

from enum import StrEnum


class EnumLandingRedClass(StrEnum):
    PRODUCT = "product"
    CASCADE = "cascade"
    RUNNER_SATURATION = "runner_saturation"
    CANCELLED_PRODUCER = "cancelled_producer"
    REVIEWER_POOL = "reviewer_pool"


__all__: list[str] = ["EnumLandingRedClass"]
