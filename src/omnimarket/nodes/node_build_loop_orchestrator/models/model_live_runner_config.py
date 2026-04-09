# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
from dataclasses import dataclass

dataclass


class ModelLiveRunnerConfig:
    orchestrator_id: str
    config_data: dict
    updated_at: str
    version: str | None = None
