# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Provisioning rule modes and skip reasons for the topic provision plan."""

from enum import StrEnum


class EnumTopicProvisionMode(StrEnum):
    """How a contract's topics are selected for provisioning."""

    ALL = "all"
    PROFILE = "profile"
    PROFILE_AND_MANIFEST = "profile_and_manifest"


class EnumTopicSkipReason(StrEnum):
    """Why a contract was not loaded under the active rule."""

    OUTSIDE_PROFILE = "outside_profile"
    NOT_IN_LANE_MANIFEST = "not_in_lane_manifest"
