# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

from pydantic import BaseModel


class OrchestratorResult(BaseModel):
    result_id: str
    success: bool
    message: str | None = None
