# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
from dataclasses import dataclass
from enum import StrEnum


class EnumPipelinePhase(StrEnum):
    """Pipeline execution phases."""

    IDLE = "idle"
    PRE_FLIGHT = "pre_flight"
    IMPLEMENT = "implement"
    LOCAL_REVIEW = "local_review"
    CREATE_PR = "create_pr"
    TEST_ITERATE = "test_iterate"
    CI_WATCH = "ci_watch"
    PR_REVIEW = "pr_review"
    AUTO_MERGE = "auto_merge"
    DONE = "done"
    FAILED = "failed"


@dataclass
class ModelPipelineState:
    pipeline_id: str
    current_phase: str
    status: str
    last_updated: str | None = None
    error: str | None = None


__all__: list[str] = ["EnumPipelinePhase", "ModelPipelineState"]
