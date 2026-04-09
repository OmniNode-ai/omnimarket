# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
from dataclasses import dataclass


@dataclass
class ModelPipelineState:
    pipeline_id: str
    current_phase: str
    status: str
    judge_verified: bool | None = None
    judge_comment: str | None = None
