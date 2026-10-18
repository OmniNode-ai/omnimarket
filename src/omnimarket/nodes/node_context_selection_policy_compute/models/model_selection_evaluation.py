# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Pinned Role C inputs and replay scoring results; no model calls or source reads."""

from __future__ import annotations

from typing import Literal

from omnibase_core.enums.enum_context_factor import EnumContextFactor
from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_context_selection_policy_compute.models.model_selection_request import (
    ModelContextCandidate,
)


class ModelSelectionEvaluationItem(BaseModel):
    """One eligible source, with the token estimate recorded by its manifest."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    candidate: ModelContextCandidate
    token_estimate: int = Field(ge=0, strict=True)


class ModelSelectionEvaluationCase(BaseModel):
    """Labels are independent of candidates, including deliberately withheld items."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    case_id: str = Field(min_length=1)
    profile_id: str = Field(min_length=1)
    manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    items: tuple[ModelSelectionEvaluationItem, ...]
    relevant_optional_sources: tuple[str, ...] = Field(min_length=1)
    resolved_required_factors: tuple[EnumContextFactor, ...]
    withheld_relevant_sources: tuple[str, ...] = ()
    token_budget: int = Field(gt=0, strict=True)
    max_optional: int = Field(ge=0, strict=True)


class ModelSelectionObservation(BaseModel):
    """One recorded selection, or one failed query that remains in the denominator."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    profile_id: str = Field(min_length=1)
    case_id: str = Field(min_length=1)
    rung: str = Field(min_length=1)
    repeat: Literal[1, 2, 3]
    status: Literal["ok", "parse_failure", "unavailable"] = "ok"
    selected_sources: tuple[str, ...] = ()
    prompt_tokens: int = Field(ge=0, strict=True)
    completion_tokens: int = Field(ge=0, strict=True)


class ModelSelectionEvaluationRequest(BaseModel):
    """The expected matrix comes from pinned manifests and the routing shortlist."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    operation: Literal["evaluate_context_selection"] = "evaluate_context_selection"
    request_id: str = ""
    profile_ids: tuple[str, ...] = Field(min_length=1)
    rungs: tuple[str, ...] = Field(min_length=1)
    cases: tuple[ModelSelectionEvaluationCase, ...] = Field(min_length=1)
    observations: tuple[ModelSelectionObservation, ...]


class ModelSelectionEvaluationRow(BaseModel):
    """A case/rung/repeat score. Package tokens are estimates, call usage is recorded."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    profile_id: str
    case_id: str
    manifest_hash: str
    rung: str
    repeat: Literal[1, 2, 3]
    status: Literal["ok", "parse_failure", "unavailable"]
    positive_control: bool
    selected_sources: tuple[str, ...]
    missed_sources: tuple[str, ...]
    relevant_count: int
    recalled_count: int
    recall: float
    package_tokens: int | None
    token_accuracy: Literal["manifest_estimate"] = "manifest_estimate"
    prompt_tokens: int
    completion_tokens: int


class ModelSelectionEvaluationSummary(BaseModel):
    """Micro recall per profile and repeat, retaining failed queries and controls."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    profile_id: str
    rung: str
    repeat: Literal[1, 2, 3]
    relevant_count: int
    recalled_count: int
    recall: float
    package_tokens: int
    prompt_tokens: int
    completion_tokens: int
    parse_failures: int
    unavailable: int


class ModelSelectionRecommendation(BaseModel):
    """Rungs eligible by recall only; this result never changes routing policy."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    profile_id: str
    eligible_rungs: tuple[str, ...]


class ModelSelectionEvaluationResult(BaseModel):
    """Invalid matrices carry errors and no partial scores or recommendations."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    request_id: str = ""
    valid: bool
    errors: tuple[str, ...] = ()
    rows: tuple[ModelSelectionEvaluationRow, ...] = ()
    summaries: tuple[ModelSelectionEvaluationSummary, ...] = ()
    recommendations: tuple[ModelSelectionRecommendation, ...] = ()
    alternatives_status: Literal["not_evaluated"] = "not_evaluated"
