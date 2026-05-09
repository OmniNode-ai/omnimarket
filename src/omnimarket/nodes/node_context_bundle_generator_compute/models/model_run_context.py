"""Run context model for context bundle generation."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelRunContext(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    session_id: str = Field(min_length=1)
    agent_id: str = ""
    timestamp: str = ""
    worker_type: str = ""
    repo: str = ""
    branch: str = ""
    trigger_event: str = ""


__all__ = ["ModelRunContext"]
