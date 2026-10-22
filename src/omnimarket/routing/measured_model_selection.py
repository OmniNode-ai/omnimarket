# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Rank delegation candidates on measured cost and measured latency (OMN-21207).

Why this exists
---------------
Every order routing used before this was declared ahead of the request: the
tier ladder in ``routing_tiers.yaml`` (OMN-8537), the user's key order
(OMN-21059). A model that has become slower or dearer keeps its place until a
human edits the order. This module ranks candidates on two MEASURES instead:

* cost per 1M tokens, read from the pricing manifest (omnibase_infra's
  ``ModelPricingTable``), blended over input and output at the contract's
  declared ``input_share``;
* p50 latency, computed from recorded delegation runs (the ``delegation_events``
  rows a delegation writes), over the contract's window.

The contract (``configs/measured_model_selection.v1.yaml``) declares, per
candidate, WHERE each measure is read from, never its value: a candidate row
carries no number, so a measure cannot be hardcoded.

Fail-closed
-----------
A candidate with no pricing entry, or with fewer recorded runs than the
contract's ``min_samples``, has a missing measure. :func:`select_by_measure`
raises :class:`MeasureMissingError` naming it. It never falls back to the
declared order, because a silent fallback is exactly the declared order this
replaces.

Decision record
---------------
:class:`ModelMeasuredSelection` records the objective, the ranking, and every
measure value it read with the time that value was captured, so the choice can
be re-derived afterwards. It rides on the published routing decision
(``ModelRoutingDecision.measured_selection``).
"""

from __future__ import annotations

import sqlite3
import statistics
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from omnibase_infra.models.pricing.model_pricing_table import ModelPricingTable
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

#: Schema tag the contract must declare.
MEASURED_SELECTION_SCHEMA_VERSION = "measured_model_selection.v1"

CONTRACT_PATH: Path = (
    Path(__file__).parent.parent / "configs" / "measured_model_selection.v1.yaml"
)


class MeasuredSelectionContractError(ValueError):
    """The selection contract is absent or mis-shaped."""


class MeasureMissingError(ValueError):
    """A candidate has no measured value for a measure the objective ranks on."""

    def __init__(self, candidate: str, measure: str, reason: str) -> None:
        self.candidate = candidate
        self.measure = measure
        self.reason = reason
        super().__init__(
            f"candidate {candidate!r} has no measured {measure}: {reason}. "
            "The selector does not fall back to the declared order."
        )


class ModelPricingManifestSource(BaseModel):
    """Where a candidate's cost per 1M tokens is read: a pricing manifest key."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: Literal["pricing_manifest"]
    #: The model key in the pricing manifest.
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

    kind: Literal["cheapest_acceptable", "fastest_acceptable"]
    #: Where the objective was approved (a knowledge-base note).
    basis: str = Field(min_length=1)


class ModelLatencyWindow(BaseModel):
    """How many recorded runs, over what window, make a p50."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    window_days: int = Field(gt=0)
    min_samples: int = Field(gt=0)


class ModelMeasuredSelectionContract(BaseModel):
    """The selector's contract: objective, measure rules, candidates."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["measured_model_selection.v1"]
    objective: ModelSelectionObjective
    #: Share of a call's tokens that are input, for the blended cost per 1M.
    input_share: float = Field(ge=0, le=1)
    latency: ModelLatencyWindow
    candidates: tuple[ModelSelectionCandidate, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _candidates_are_distinct(self) -> ModelMeasuredSelectionContract:
        keys = [(c.model_id, c.backend_ref) for c in self.candidates]
        if len(set(keys)) != len(keys):
            raise ValueError("a candidate (model_id, backend_ref) is declared twice")
        return self


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

    objective: Literal["cheapest_acceptable", "fastest_acceptable"]
    selected_model: str
    selected_backend_ref: str
    #: Every candidate's measures, best first.
    ranked: tuple[ModelCandidateMeasures, ...] = Field(min_length=1)
    decided_at: datetime


def _read_contract(path: Path) -> ModelMeasuredSelectionContract:
    if not path.is_file():
        raise MeasuredSelectionContractError(
            f"measured model selection contract not found at {path}"
        )
    raw: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise MeasuredSelectionContractError(f"contract at {path} is not a mapping")
    try:
        return ModelMeasuredSelectionContract.model_validate(raw)
    except ValidationError as exc:
        raise MeasuredSelectionContractError(
            f"contract at {path} is mis-shaped: {exc}"
        ) from exc


@lru_cache(maxsize=1)
def load_measured_selection_contract() -> ModelMeasuredSelectionContract:
    """Load and cache the shipped selection contract."""
    return _read_contract(CONTRACT_PATH)


def _effective_date_utc(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=UTC)


def measure_candidates(
    contract: ModelMeasuredSelectionContract,
    pricing: ModelPricingTable,
    runs: Iterable[ModelRecordedRun],
    *,
    now: datetime,
) -> tuple[ModelCandidateMeasures, ...]:
    """Read every candidate's cost and p50 latency; raise on the first missing one."""
    window_start = now - timedelta(days=contract.latency.window_days)
    by_route: dict[tuple[str, str], list[ModelRecordedRun]] = {}
    for run in runs:
        if run.recorded_at >= window_start:
            by_route.setdefault((run.backend_id, run.model_name), []).append(run)

    measured: list[ModelCandidateMeasures] = []
    for candidate in contract.candidates:
        cost_ref = candidate.measured_cost_per_1m
        entry = pricing.get_entry(cost_ref.pricing_key)
        if entry is None:
            raise MeasureMissingError(
                candidate.model_id,
                "cost_per_1m",
                f"the pricing manifest has no entry {cost_ref.pricing_key!r}",
            )
        cost_per_1m = 1000 * (
            entry.input_cost_per_1k * contract.input_share
            + entry.output_cost_per_1k * (1 - contract.input_share)
        )

        latency_ref = candidate.measured_p50_latency
        samples = by_route.get((latency_ref.backend_id, latency_ref.model_name), [])
        if len(samples) < contract.latency.min_samples:
            raise MeasureMissingError(
                candidate.model_id,
                "p50_latency",
                f"{len(samples)} recorded runs on backend "
                f"{latency_ref.backend_id!r} model {latency_ref.model_name!r} in "
                f"the last {contract.latency.window_days} days, fewer than "
                f"min_samples {contract.latency.min_samples}",
            )
        measured.append(
            ModelCandidateMeasures(
                model_id=candidate.model_id,
                backend_ref=candidate.backend_ref,
                cost_per_1m_usd=cost_per_1m,
                cost_source=f"pricing_manifest:{cost_ref.pricing_key}",
                cost_captured_at=_effective_date_utc(entry.effective_date),
                p50_latency_ms=statistics.median(s.latency_ms for s in samples),
                latency_sample_count=len(samples),
                latency_captured_at=max(s.recorded_at for s in samples),
            )
        )
    return tuple(measured)


def select_by_measure(
    contract: ModelMeasuredSelectionContract,
    pricing: ModelPricingTable,
    runs: Iterable[ModelRecordedRun],
    *,
    now: datetime | None = None,
) -> ModelMeasuredSelection:
    """Rank the contract's candidates under its objective and record the decision.

    Raises:
        MeasureMissingError: a candidate has no measured cost or latency.
    """
    decided_at = now or datetime.now(tz=UTC)
    measured = measure_candidates(contract, pricing, runs, now=decided_at)
    if contract.objective.kind == "cheapest_acceptable":
        ranked = sorted(measured, key=lambda m: (m.cost_per_1m_usd, m.p50_latency_ms))
    else:
        ranked = sorted(measured, key=lambda m: (m.p50_latency_ms, m.cost_per_1m_usd))
    best = ranked[0]
    return ModelMeasuredSelection(
        objective=contract.objective.kind,
        selected_model=best.model_id,
        selected_backend_ref=best.backend_ref,
        ranked=tuple(ranked),
        decided_at=decided_at,
    )


def read_recorded_runs(
    database: Path, *, since: datetime
) -> tuple[ModelRecordedRun, ...]:
    """Read recorded run latencies from a ``delegation_events`` SQLite table.

    Only terminal-ok rows that carry a latency count; a row whose latency is
    null or whose timestamp cannot be read is skipped, never guessed.
    """
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as conn:
        rows = conn.execute(
            "SELECT backend_id, model_name, "
            "COALESCE(delegation_latency_ms, latency_ms), timestamp "
            "FROM delegation_events WHERE terminal_ok = 1 AND timestamp >= ?",
            (since.isoformat(),),
        ).fetchall()
    runs: list[ModelRecordedRun] = []
    for backend_id, model_name, latency_ms, timestamp in rows:
        if not backend_id or not model_name or latency_ms is None:
            continue
        try:
            recorded_at = datetime.fromisoformat(str(timestamp))
        except ValueError:
            continue
        if recorded_at.tzinfo is None:
            recorded_at = recorded_at.replace(tzinfo=UTC)
        runs.append(
            ModelRecordedRun(
                backend_id=str(backend_id),
                model_name=str(model_name),
                latency_ms=int(latency_ms),
                recorded_at=recorded_at,
            )
        )
    return tuple(runs)


def measures_by_model(
    selection: ModelMeasuredSelection,
) -> Mapping[str, ModelCandidateMeasures]:
    """The decision's measures keyed by ``model_id@backend_ref``."""
    return {f"{m.model_id}@{m.backend_ref}": m for m in selection.ranked}


__all__ = [
    "CONTRACT_PATH",
    "MEASURED_SELECTION_SCHEMA_VERSION",
    "MeasureMissingError",
    "MeasuredSelectionContractError",
    "ModelCandidateMeasures",
    "ModelDelegationRunsSource",
    "ModelLatencyWindow",
    "ModelMeasuredSelection",
    "ModelMeasuredSelectionContract",
    "ModelPricingManifestSource",
    "ModelRecordedRun",
    "ModelSelectionCandidate",
    "ModelSelectionObjective",
    "load_measured_selection_contract",
    "measure_candidates",
    "measures_by_model",
    "read_recorded_runs",
    "select_by_measure",
]
