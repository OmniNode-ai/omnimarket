# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
from dataclasses import dataclass

dataclass


class ModelPhaseCommandIntent:
    orchestrator_id: str
    phase_name: str
    intent_time: str
    pipeline_id: str | None = None
