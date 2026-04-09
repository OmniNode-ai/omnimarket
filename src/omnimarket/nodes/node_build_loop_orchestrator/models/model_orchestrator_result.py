# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

from pydantic import BaseModel


class OrchestratorResult(BaseModel):
    result_id: str
    outcome: str
    processed_at: str | None = None
