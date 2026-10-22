# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed shapes for measured model selection (OMN-21207).

The selection rules live in the ``measured_selection`` block of
``node_measured_model_selection_compute/contract.yaml``. The candidates are the
models of the routing contract's (``routing_tiers.yaml``) named tiers. Each
candidate carries WHERE its measures are read (a pricing manifest key, a backend
and model in recorded delegation runs), never a value: every model is
``extra="forbid"``, so a literal cannot be added beside a source.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

EnumSelectionObjective = Literal["cheapest_acceptable", "fastest_acceptable"]


class ModelPricingManifestSource(BaseModel):
    """Where a candidate's cost per 1M tokens is read: a pricing manifest key."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: Literal["pricing_manifest"]
    pricing_key: str = Field(min_length=1)


class ModelDelegationRunsSource(BaseModel):
    """Where a candidate's p50 latency is read: recorded delegation runs."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: Literal["delegation_runs"]
    #: The ``backend_id`` the runs were answered on.
    backend_id: str = Field(min_length=1)
    #: The ``model_name`` the runs recorded.
    model_name: str = Field(min_length=1)


class ModelSelectionCandidate(BaseModel):
    """One candidate model and where each of its measures comes from."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model_id: str = Field(min_length=1)
    backend_ref: str = Field(min_length=1)
    measured_cost_per_1m: ModelPricingManifestSource
    measured_p50_latency: ModelDelegationRunsSource


class ModelSelectionObjective(BaseModel):
    """The one objective the selector optimises.

    ``cheapest_acceptable`` ranks on cost, then latency; ``fastest_acceptable``
    on latency, then cost. "Acceptable" is decided downstream: the ladder moves
    to the next ranked candidate when the quality gate refuses an answer.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: EnumSelectionObjective
    #: The ticket whose record carries the operator's approval.
    basis: str = Field(pattern=r"^OMN-[0-9]+$")


class ModelLatencyWindow(BaseModel):
    """How many recorded runs, over what window, make a p50."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    window_days: int = Field(gt=0)
    min_samples: int = Field(gt=0)


class ModelCandidateRule(BaseModel):
    """Which routing-contract models are candidates, and where their measures are read.

    Each model of a named tier becomes a candidate. Its cost is the pricing
    manifest entry keyed by its model id; its latency is the recorded runs on
    its backend_ref under its model id.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: Literal["routing_tiers"]
    tiers: tuple[str, ...] = Field(min_length=1)
    measured_cost_per_1m: Literal["pricing_manifest"]
    measured_p50_latency: Literal["delegation_runs"]


class ModelMeasuredSelectionContract(BaseModel):
    """The selector's rules: objective, measure windows, candidate rule."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    objective: ModelSelectionObjective
    #: Share of a call's tokens that are input, for the blended cost per 1M.
    input_share: float = Field(ge=0, le=1)
    latency: ModelLatencyWindow
    candidates: ModelCandidateRule


class ModelPricedModel(BaseModel):
    """One pricing manifest entry, as the selector reads it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pricing_key: str = Field(min_length=1)
    input_cost_per_1k: float = Field(ge=0)
    output_cost_per_1k: float = Field(ge=0)
    #: When the price was captured (the manifest entry's effective date).
    captured_at: datetime


class ModelRecordedRun(BaseModel):
    """One recorded delegation run's latency."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    backend_id: str
    model_name: str
    latency_ms: int = Field(ge=0)
    recorded_at: datetime


class ModelCandidateMeasures(BaseModel):
    """The measure values one candidate was ranked on, and when each was captured."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model_id: str
    backend_ref: str
    cost_per_1m_usd: float = Field(ge=0)
    cost_source: str
    cost_captured_at: datetime
    p50_latency_ms: float = Field(ge=0)
    latency_sample_count: int = Field(gt=0)
    latency_captured_at: datetime


class ModelMeasuredSelection(BaseModel):
    """The decision record: what was chosen, on which measures, captured when."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    objective: EnumSelectionObjective
    selected_model: str
    selected_backend_ref: str
    #: Every candidate's measures, best first.
    ranked: tuple[ModelCandidateMeasures, ...] = Field(min_length=1)
    decided_at: datetime


__all__ = [
    "EnumSelectionObjective",
    "ModelCandidateMeasures",
    "ModelCandidateRule",
    "ModelDelegationRunsSource",
    "ModelLatencyWindow",
    "ModelMeasuredSelection",
    "ModelMeasuredSelectionContract",
    "ModelPricedModel",
    "ModelPricingManifestSource",
    "ModelRecordedRun",
    "ModelSelectionCandidate",
    "ModelSelectionObjective",
]
