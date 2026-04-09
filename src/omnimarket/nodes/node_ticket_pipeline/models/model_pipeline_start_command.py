# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
from dataclasses import dataclass


@dataclass
class ModelPipelineStartCommand:
    pipeline_id: str
    ticket_id: str
    reviewer_id: str | None = None
    judge_id: str | None = None
