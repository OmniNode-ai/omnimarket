# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
from dataclasses import dataclass

dataclass


class ModelOrchestratorStartCommand:
    orchestrator_id: str
    start_time: str
    pipeline_id: str | None = None
