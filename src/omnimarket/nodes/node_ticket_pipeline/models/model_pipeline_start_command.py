# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
from pydantic import BaseModel


class PipelineStartCommand(BaseModel):
    pipeline_id: str
    command: str
    started_at: str
