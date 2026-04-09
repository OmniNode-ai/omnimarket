# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
from dataclasses import dataclass

default_phase_event = {
    "event_type": "pipeline_phase",
    "phase": "post_merge_verification",
    "timestamp": "2026-04-08T00:00:00Z",
    "status": "in_progress",
}


@dataclass
class PipelinePhaseEvent:
    event_type: str = "pipeline_phase"
    phase: str = "post_merge_verification"
    timestamp: str = "2026-04-08T00:00:00Z"
    status: str = "in_progress"
    message: str | None = None
