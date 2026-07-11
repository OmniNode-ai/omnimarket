"""Request model for the AST node analyzer compute node."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelAstNodeAnalyzerRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    source_text: str = Field(min_length=1)


__all__ = ["ModelAstNodeAnalyzerRequest"]
