# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
from pydantic import BaseModel


class LoopCycleSummary(BaseModel):
    cycle_id: str
    status: str
    summary: str
    completed_at: str
