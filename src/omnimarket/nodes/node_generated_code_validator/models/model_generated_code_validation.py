"""Result model: typed diagnostics for a generated code artifact."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ModelGeneratedCodeValidation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    parses: bool
    syntax_error: str | None
    stub_methods: tuple[str, ...]
    structure_issues: tuple[str, ...]
    is_valid: bool
    correlation_id: str = ""  # echoed from the request; OMN-14608 reducer join key


__all__ = ["ModelGeneratedCodeValidation"]
