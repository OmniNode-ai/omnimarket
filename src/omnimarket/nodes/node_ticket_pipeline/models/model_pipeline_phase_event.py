# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
from dataclasses import dataclass


@dataclass
class ModelPipelinePhaseEvent:
    pipeline_id: str
    phase: str
    event_type: str
    timestamp: str | None = None
    details: dict | None = None
