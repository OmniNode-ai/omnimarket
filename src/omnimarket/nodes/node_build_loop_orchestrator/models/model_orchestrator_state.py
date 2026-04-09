# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

from pydantic import BaseModel


class OrchestratorState(BaseModel):
    orchestrator_id: str
    current_phase: str | None = None
    status: str
