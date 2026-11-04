# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""EnumRunStyle: the one automation-versus-human split any plan uses (closed enum)."""

from __future__ import annotations

from enum import StrEnum


class EnumRunStyle(StrEnum):
    """The eight ways of running work the calibration fold contrasts."""

    FOREGROUND = "foreground"
    WORKFLOW = "workflow"
    AGENT = "agent"
    REMOTE_LANE = "remote_lane"
    LAB_RUN = "lab_run"
    CODEX = "codex"
    GLM = "glm"
    LAB_DELEGATION = "lab_delegation"


__all__ = ["EnumRunStyle"]
