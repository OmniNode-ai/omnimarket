# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed recorded failures in, a typed commentary account out (OMN-18538).

Every output model carries the commentary marker as a constant the type system fixes, so an output
cannot be built without it and cannot be built with any other value.
"""

from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

COMMENTARY_MARKER: Final = "onex.commentary.omnimarket.not_evidence.v1"
"""The machine-readable marker on every output; the contract's explanation_rules repeats it."""

CommentaryMarker = Literal["onex.commentary.omnimarket.not_evidence.v1"]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ModelRecordedDelegationFailure(_Frozen):
    """One failure as already recorded. Each field comes from a surface the contract names."""

    correlation_id: str = Field(min_length=1)
    terminal_status: str = ""
    terminal_failure_cause: str = ""
    step_kind: str = ""
    failure_cause: str = ""
    backend_id: str = ""
    reason: str = ""

    @field_validator("correlation_id")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("correlation_id must not be blank")
        return value


class ModelDelegationFailureExplanationRequest(_Frozen):
    """Recorded failures read by the caller; this node explains them and reads nothing itself."""

    correlation_id: str = Field(min_length=1)
    failures: tuple[ModelRecordedDelegationFailure, ...] = ()

    @field_validator("correlation_id")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("correlation_id must not be blank")
        return value


class ModelFailureGroupFacts(_Frozen):
    """The recorded facts of one group: what a narrator may be given, and nothing else."""

    group_key: str
    cause_recorded: bool
    count: int = Field(ge=1)
    correlation_ids: tuple[str, ...]
    statuses: tuple[str, ...] = ()
    backends: tuple[str, ...] = ()
    steps: tuple[str, ...] = ()


class ModelFailureAccountGroup(_Frozen):
    """One account: its key is recorded metadata, its text is commentary."""

    commentary_marker: CommentaryMarker = COMMENTARY_MARKER
    is_evidence: Literal[False] = False
    group_key: str
    cause_recorded: bool
    count: int = Field(ge=1)
    correlation_ids: tuple[str, ...]
    account: str


class ModelDelegationFailureExplanation(_Frozen):
    """The whole commentary: groups by recorded cause, and the marker on the whole and each group."""

    commentary_marker: CommentaryMarker = COMMENTARY_MARKER
    is_evidence: Literal[False] = False
    correlation_id: str
    groups: tuple[ModelFailureAccountGroup, ...] = ()
    total_failures: int = Field(ge=0)
    summary: str


class ModelExplanationTemplates(_Frozen):
    """The contract's wording."""

    known: str = Field(min_length=1)
    unknown: str = Field(min_length=1)
    none_listed: str = Field(min_length=1)
    summary: str = Field(min_length=1)


class ModelExplanationRules(_Frozen):
    """The contract's explanation_rules: every value the account varies with."""

    commentary_marker: CommentaryMarker
    held_surfaces: dict[str, str]
    group_key_fields: tuple[str, ...] = Field(min_length=1)
    unknown_group_key: str = Field(min_length=1)
    max_listed_correlation_ids: int = Field(ge=1)
    templates: ModelExplanationTemplates

    @field_validator("group_key_fields")
    @classmethod
    def _known_fields(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        unknown = [
            f for f in value if f not in ModelRecordedDelegationFailure.model_fields
        ]
        if unknown:
            raise ValueError(f"group_key_fields not recorded fields: {unknown}")
        return value
