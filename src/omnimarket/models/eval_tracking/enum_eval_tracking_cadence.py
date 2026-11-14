# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Which cadence an arm of the tracking record runs on (OMN-24177, plan task T13)."""

from __future__ import annotations

from enum import StrEnum


class EnumEvalTrackingCadence(StrEnum):
    """A local model runs every ``local_days``; a hosted model every ``glm_days``."""

    LOCAL = "local"
    GLM = "glm"


__all__ = ["EnumEvalTrackingCadence"]
