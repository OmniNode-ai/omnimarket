# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
from dataclasses import dataclass

default_completed_event = {
    "event_type": "pipeline_completed",
    "phase": "post_merge_verification",
    "timestamp": "2026-04-08T00:00:00Z",
    "status": "success",
}


@dataclass
class PipelineCompletedEvent:
    event_type: str = "pipeline_completed"
    phase: str = "post_merge_verification"
    timestamp: str = "2026-04-08T00:00:00Z"
    status: str = "success"
    message: str | None = None
