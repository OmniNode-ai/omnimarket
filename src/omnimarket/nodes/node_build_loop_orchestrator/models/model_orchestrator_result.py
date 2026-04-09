# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
from dataclasses import dataclass

dataclass


class ModelOrchestratorResult:
    orchestrator_id: str
    result_data: dict
    processed_at: str
    status: str | None = None
