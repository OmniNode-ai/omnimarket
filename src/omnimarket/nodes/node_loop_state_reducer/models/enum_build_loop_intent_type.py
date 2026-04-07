# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Build loop intent type enumeration.

Related:
    - OMN-7311: ModelBuildLoopState foundation models
    - OMN-5113: Autonomous Build Loop epic
    - OMN-7752: Migrated from omnibase_infra
"""

from __future__ import annotations

from enum import Enum


class EnumBuildLoopIntentType(str, Enum):
    """Intent types emitted by the build loop reducer.

    Each intent type maps to a specific downstream node invocation
    that the orchestrator routes to the appropriate effect or compute node.
    """

    START_CLOSEOUT = "build_loop.start_closeout"
    START_DEPLOY = "build_loop.start_deploy"
    START_VERIFY = "build_loop.start_verify"
    START_FILL = "build_loop.start_fill"
    START_CLASSIFY = "build_loop.start_classify"
    START_BUILD = "build_loop.start_build"
    CYCLE_COMPLETE = "build_loop.cycle_complete"
    CIRCUIT_BREAK = "build_loop.circuit_break"


__all__: list[str] = ["EnumBuildLoopIntentType"]
