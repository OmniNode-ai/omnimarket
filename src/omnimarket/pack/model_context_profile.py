"""Contract-governed context profile input for context-pack assembly."""

from __future__ import annotations

import hashlib
import json
import re

from omnibase_core.enums.enum_context_factor import EnumContextFactor
from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.enums.enum_context_binding_kind import EnumContextBindingKind
from omnimarket.models.context_pack.model_context_task_kind_rule import (
    ModelContextTaskKindRule,
)

#: Factors of plan section 4.2 rows 1 to 5: bytes depend only on the pinned commit.
STABLE_TIER_FACTORS: frozenset[EnumContextFactor] = frozenset(
    {
        EnumContextFactor.LANE_RULES,
        EnumContextFactor.INVARIANTS,
        EnumContextFactor.ADRS,
        EnumContextFactor.CANONICAL_SHAPES,
        EnumContextFactor.ARCHITECTURE_PATTERNS,
        EnumContextFactor.EXEMPLAR,
    }
)
#: Factors a model writes (row 10); they render last and are trimmed first.
MODEL_WRITTEN_FACTORS: frozenset[EnumContextFactor] = frozenset(
    {EnumContextFactor.TAIL_SUMMARY}
)
_TASK_CLASS_SHAPE = re.compile(r"^[a-z]+(_[a-z]+)*$")
_TASK_CLASS_FIELDS = (
    "selection_task_class",
    "extraction_task_class",
    "summary_task_class",
)


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
    conversation_tail_messages: int | None = Field(default=None, gt=0)
    injection_point: str | None = Field(default=None, min_length=1)
    binding_kind: EnumContextBindingKind | None = None
    scopes: tuple[str, ...] = Field(default_factory=tuple)
    adr_topics: tuple[str, ...] = Field(default_factory=tuple)
    task_kind_rules: tuple[ModelContextTaskKindRule, ...] = Field(default_factory=tuple)
    stable_factors: tuple[EnumContextFactor, ...] = Field(default_factory=tuple)
    selection_task_class: str | None = None
    extraction_task_class: str | None = None
    summary_task_class: str | None = None
    latency_budget_ms: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _validate_factor_policy(self) -> ModelContextProfile:
        required = set(self.required_factors)
        optional = set(self.optional_factors)
        excluded = set(self.excluded_factors)
        if required & excluded:
            raise ValueError("required_factors cannot also be excluded")
        if optional & excluded:
            raise ValueError("optional_factors cannot also be excluded")
        selected = required | optional
        if (
            EnumContextFactor.CONVERSATION_TAIL in selected
            and self.conversation_tail_messages is None
        ):
            raise ValueError(
                "conversation_tail_messages is required when CONVERSATION_TAIL "
                "is in required_factors or optional_factors"
            )
        return self

    @model_validator(mode="after")
    def _validate_precedence(self) -> ModelContextProfile:
        stable = set(self.stable_factors)
        if not stable <= STABLE_TIER_FACTORS:
            names = sorted(f.value for f in stable - STABLE_TIER_FACTORS)
            raise ValueError(f"stable_factors names non-stable-tier factors: {names}")
        if stable & set(self.excluded_factors):
            raise ValueError("stable_factors cannot also be excluded")
        seen_non_stable = False
        for factor in self.factor_precedence:
            if factor in stable:
                if seen_non_stable:
                    raise ValueError(
                        f"factor_precedence puts a non-stable factor before "
                        f"stable factor {factor.value}"
                    )
            else:
                seen_non_stable = True
        precedence = self.factor_precedence
        for index, factor in enumerate(precedence):
            if factor in MODEL_WRITTEN_FACTORS and index != len(precedence) - 1:
                raise ValueError(
                    f"model-written factor {factor.value} must be last in "
                    f"factor_precedence"
                )
        return self

    @model_validator(mode="after")
    def _validate_task_classes_and_latency(self) -> ModelContextProfile:
        for field_name in _TASK_CLASS_FIELDS:
            value = getattr(self, field_name)
            if value is None:
                continue
            if not _TASK_CLASS_SHAPE.fullmatch(value) or value == self.model_id:
                raise ValueError(
                    f"{field_name} must name a task class (lowercase words joined "
                    f"by underscores), never a model id: {value!r}"
                )
        if (
            self.latency_budget_ms is not None
            and self.binding_kind is not EnumContextBindingKind.ADDITIVE
        ):
            raise ValueError("latency_budget_ms is declared only by additive profiles")
        return self

    def content_hash(self) -> str:
        """sha256 over the profile's canonical JSON; equal profiles hash equal."""
        raw = json.dumps(
            self.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


__all__ = [
    "MODEL_WRITTEN_FACTORS",
    "STABLE_TIER_FACTORS",
    "ModelContextProfile",
]
