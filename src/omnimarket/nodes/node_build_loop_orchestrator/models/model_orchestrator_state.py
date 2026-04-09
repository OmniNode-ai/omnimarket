# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

from pydantic import BaseModel


class OrchestratorState(BaseModel):
    orchestrator_id: str
    status: str
    current_phase: str | None = None
    updated_at: str
