"""Request model for the mypy check effect node."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ModelMypyCheckRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    source_text: str | None = Field(
        default=None,
        description="Artifact source to type-check; written to a temp file when set.",
    )
    path: str | None = Field(
        default=None,
        description="Path to an existing file or directory to type-check.",
    )
    ignore_missing_imports: bool = Field(
        default=True,
        description="Pass --ignore-missing-imports (generated snippets often lack third-party stubs).",
    )

    @model_validator(mode="after")
    def _exactly_one_target(self) -> ModelMypyCheckRequest:
        if (self.source_text is None) == (self.path is None):
            raise ValueError("exactly one of source_text or path must be provided")
        if self.source_text is not None and not self.source_text.strip():
            raise ValueError("source_text must not be empty")
        return self


__all__ = ["ModelMypyCheckRequest"]
