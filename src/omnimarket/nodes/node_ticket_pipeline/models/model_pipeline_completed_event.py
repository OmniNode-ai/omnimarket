# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
from dataclasses import dataclass


@dataclass
class ModelPipelineCompletedEvent:
    pipeline_id: str
    status: str
    completed_at: str
    judge_verification: bool | None = None
    judge_comment: str | None = None
