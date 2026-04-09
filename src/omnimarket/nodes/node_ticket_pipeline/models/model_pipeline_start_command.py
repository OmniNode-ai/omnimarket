# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
from dataclasses import dataclass


@dataclass
class ModelPipelineStartCommand:
    pipeline_id: str
    phase: str
    started_at: str | None = None
    metadata: dict | None = None
