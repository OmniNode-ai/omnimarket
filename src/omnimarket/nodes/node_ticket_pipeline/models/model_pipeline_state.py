# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
from dataclasses import dataclass


@dataclass
class ModelPipelineState:
    pipeline_id: str
    current_phase: str
    status: str
    last_updated: str | None = None
    error: str | None = None
