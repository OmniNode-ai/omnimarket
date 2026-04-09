# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

from pydantic import BaseModel


class PipelinePhaseEvent(BaseModel):
    pipeline_id: str
    phase: str
    event_time: str | None = None
