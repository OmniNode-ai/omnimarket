# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

from pydantic import BaseModel


class OrchestratorStartCommand(BaseModel):
    orchestrator_id: str
    start_time: str | None = None
