# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
from pydantic import BaseModel


class OrchestratorStartCommand(BaseModel):
    orchestrator_id: str
    command: str
    started_at: str
