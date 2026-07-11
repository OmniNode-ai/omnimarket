"""Request model for the stub detector compute node."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelStubDetectorRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    source_text: str = Field(min_length=1)


__all__ = ["ModelStubDetectorRequest"]
