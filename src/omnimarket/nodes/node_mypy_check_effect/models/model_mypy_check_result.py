"""Result model: typed mypy diagnostics for a checked artifact."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ModelMypyDiagnostic(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    line: int
    column: int | None
    severity: str
    message: str
    code: str | None


class ModelMypyCheckResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    success: bool
    error_count: int
    diagnostics: tuple[ModelMypyDiagnostic, ...]
    mypy_available: bool


__all__ = ["ModelMypyCheckResult", "ModelMypyDiagnostic"]
