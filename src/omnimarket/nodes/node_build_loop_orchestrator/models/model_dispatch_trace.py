# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
from dataclasses import dataclass

dataclass


class ModelDispatchTrace:
    orchestrator_id: str
    trace_data: dict
    recorded_at: str
    source: str | None = None
