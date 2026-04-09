# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
from pydantic import BaseModel


class OrchestratorCompletedEvent(BaseModel):
    orchestrator_id: str
    status: str
    completed_at: str
