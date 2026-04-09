# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

from pydantic import BaseModel


class OrchestratorCompletedEvent(BaseModel):
    orchestrator_id: str
    completion_time: str | None = None
    status: str
