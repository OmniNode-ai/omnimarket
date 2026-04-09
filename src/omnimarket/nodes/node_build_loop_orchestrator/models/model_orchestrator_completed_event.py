# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
from dataclasses import dataclass

dataclass


class ModelOrchestratorCompletedEvent:
    orchestrator_id: str
    completed_at: str
    status: str
    metrics: dict | None = None
