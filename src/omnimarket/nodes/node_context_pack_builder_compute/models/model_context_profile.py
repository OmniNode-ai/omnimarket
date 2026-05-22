"""Contract-governed context profile input for context-pack assembly."""

from __future__ import annotations

from omnibase_core.enums.enum_context_factor import EnumContextFactor
from pydantic import BaseModel, ConfigDict, Field, model_validator


class ModelContextProfile(BaseModel):
    """Validated profile controlling factor policy and token budget."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    profile_schema_version: str = Field(default="1.0.0", min_length=1)
    profile_version: str = Field(default="1.0.0", min_length=1)
    model_id: str = Field(min_length=1)
    required_factors: tuple[EnumContextFactor, ...] = Field(default_factory=tuple)
    optional_factors: tuple[EnumContextFactor, ...] = Field(default_factory=tuple)
    excluded_factors: tuple[EnumContextFactor, ...] = Field(default_factory=tuple)
    factor_precedence: tuple[EnumContextFactor, ...] = Field(default_factory=tuple)
    token_budget: int = Field(default=16000, gt=0)
    token_estimation_method: str = Field(default="heuristic_chars", min_length=1)
    tokenizer_source: str = Field(default="heuristic", min_length=1)
    tokenizer_version: str = Field(default="1.0.0", min_length=1)
    estimation_accuracy: str = Field(default="estimated", min_length=1)

    @model_validator(mode="after")
    def _validate_factor_policy(self) -> ModelContextProfile:
        required = set(self.required_factors)
        optional = set(self.optional_factors)
        excluded = set(self.excluded_factors)
        if required & excluded:
            raise ValueError("required_factors cannot also be excluded")
        if optional & excluded:
            raise ValueError("optional_factors cannot also be excluded")
        return self


__all__ = ["ModelContextProfile"]
