# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
from dataclasses import dataclass

dataclass


class ModelPipelinePhaseEvent:
    pipeline_id: str
    phase_name: str
    started_at: str
    completed_at: str | None = None
    status: str | None = None
