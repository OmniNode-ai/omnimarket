# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
from dataclasses import dataclass

dataclass


class ModelPipelineStartCommand:
    pipeline_id: str
    start_time: str
    orchestrator_id: str | None = None
