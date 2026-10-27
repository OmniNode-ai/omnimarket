# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Rank delegation candidates on measured cost and measured latency (OMN-21207).

Every order routing used before this was declared ahead of the request: the
tier ladder in ``routing_tiers.yaml`` (OMN-8537), the user's key order
(OMN-21059). A model that has become slower or dearer keeps its place until a
human edits the order. This handler ranks candidates on two MEASURES instead:
cost per 1M tokens from the pricing manifest, and p50 latency from recorded
delegation runs. The node contract's ``measured_selection`` block declares,
per candidate, where each is read, never its value.

Fail-closed: a candidate with no pricing entry, or fewer recorded runs in the
window than ``min_samples``, raises :class:`MeasureMissingError` naming it. It
never falls back to the declared order, because a silent fallback is exactly
the declared order this replaces.

Pure: the caller supplies the candidates, the prices and the runs.
:func:`candidates_from_routing_tiers`, :func:`priced_models_from_manifest` and
:func:`recorded_runs_from_rows` build them from the routing contract
(``routing_tiers.yaml``), the pricing manifest and ``delegation_events`` rows.
"""

from __future__ import annotations

import statistics
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from omnibase_infra.models.pricing.model_pricing_table import ModelPricingTable

from omnimarket.handlers.handler_measured_selection_config import (
    MeasuredSelectionContractError,
    load_measured_selection_contract,
)
from omnimarket.models.delegation.model_measured_selection import (
    ModelCandidateMeasures,
    ModelCandidateRule,
    ModelDelegationRunsSource,
    ModelMeasuredSelection,
    ModelMeasuredSelectionContract,
    ModelPricedModel,
    ModelPricingManifestSource,
    ModelRecordedRun,
    ModelSelectionCandidate,
)
from omnimarket.models.delegation.wire.model_measured_selection_decided_event import (
    ModelMeasuredSelectionDecidedEvent,
    ModelMeasuredSelectionRequest,
)


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


def candidates_from_routing_tiers(
    rule: ModelCandidateRule, routing_tiers: Mapping[str, Any]
) -> tuple[ModelSelectionCandidate, ...]:
    """Every model of the rule's tiers in a parsed ``routing_tiers.yaml``.

    One candidate per distinct (model id, backend_ref). Raises
    :class:`MeasuredSelectionContractError` when a named tier is absent.
    """
    tiers = {
        str(tier.get("name")): tier
        for tier in routing_tiers.get("tiers") or ()
        if isinstance(tier, Mapping)
    }
    candidates: dict[tuple[str, str], ModelSelectionCandidate] = {}
    for name in rule.tiers:
        if name not in tiers:
            raise MeasuredSelectionContractError(
                f"routing contract has no tier {name!r} named by measured_selection"
            )
        for model in tiers[name].get("models") or ():
            model_id, backend_ref = str(model["id"]), str(model["backend_id"])
            candidates.setdefault(
                (model_id, backend_ref),
                ModelSelectionCandidate(
                    model_id=model_id,
                    backend_ref=backend_ref,
                    measured_cost_per_1m=ModelPricingManifestSource(
                        source=rule.measured_cost_per_1m, pricing_key=model_id
                    ),
                    measured_p50_latency=ModelDelegationRunsSource(
                        source=rule.measured_p50_latency,
                        backend_id=backend_ref,
                        model_name=model_id,
                    ),
                ),
            )
    return tuple(candidates.values())


def priced_models_from_manifest(
    table: ModelPricingTable,
) -> tuple[ModelPricedModel, ...]:
    """Every pricing manifest entry, captured at its effective date."""
    return tuple(
        ModelPricedModel(
            pricing_key=key,
            input_cost_per_1k=entry.input_cost_per_1k,
            output_cost_per_1k=entry.output_cost_per_1k,
            captured_at=datetime.strptime(entry.effective_date, "%Y-%m-%d").replace(
                tzinfo=UTC
            ),
        )
        for key, entry in table.models.items()
    )


def recorded_runs_from_rows(
    rows: Iterable[Mapping[str, object]],
) -> tuple[ModelRecordedRun, ...]:
    """Recorded run latencies from ``delegation_events`` rows.

    Only terminal-ok rows that carry a backend, a model, a latency
    (``delegation_latency_ms``, else ``latency_ms``) and a readable timestamp
    count; any other row is skipped, never guessed.
    """
    runs: list[ModelRecordedRun] = []
    for row in rows:
        latency = row.get("delegation_latency_ms")
        if latency is None:
            latency = row.get("latency_ms")
        backend_id, model_name = row.get("backend_id"), row.get("model_name")
        if (
            not row.get("terminal_ok")
            or not backend_id
            or not model_name
            or not isinstance(latency, int)
        ):
            continue
        try:
            recorded_at = datetime.fromisoformat(str(row.get("timestamp")))
        except ValueError:
            continue
        if recorded_at.tzinfo is None:
            recorded_at = recorded_at.replace(tzinfo=UTC)
        runs.append(
            ModelRecordedRun(
                backend_id=str(backend_id),
                model_name=str(model_name),
                latency_ms=latency,
                recorded_at=recorded_at,
            )
        )
    return tuple(runs)


def measure_candidates(
    contract: ModelMeasuredSelectionContract,
    candidates: Iterable[ModelSelectionCandidate],
    pricing: Iterable[ModelPricedModel],
    runs: Iterable[ModelRecordedRun],
    *,
    now: datetime,
) -> tuple[ModelCandidateMeasures, ...]:
    """Read every candidate's cost and p50 latency; raise on the first missing one."""
    prices = {price.pricing_key: price for price in pricing}
    window_start = now - timedelta(days=contract.latency.window_days)
    by_route: dict[tuple[str, str], list[ModelRecordedRun]] = {}
    for run in runs:
        if window_start <= run.recorded_at <= now:
            by_route.setdefault((run.backend_id, run.model_name), []).append(run)

    measured: list[ModelCandidateMeasures] = []
    for candidate in candidates:
        cost_ref = candidate.measured_cost_per_1m
        price = prices.get(cost_ref.pricing_key)
        if price is None:
            raise MeasureMissingError(
                candidate.model_id,
                "cost_per_1m",
                f"the pricing manifest has no entry {cost_ref.pricing_key!r}",
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
                cost_per_1m_usd=1000
                * (
                    price.input_cost_per_1k * contract.input_share
                    + price.output_cost_per_1k * (1 - contract.input_share)
                ),
                cost_source=f"pricing_manifest:{cost_ref.pricing_key}",
                cost_captured_at=price.captured_at,
                p50_latency_ms=statistics.median(s.latency_ms for s in samples),
                latency_sample_count=len(samples),
                latency_captured_at=max(s.recorded_at for s in samples),
            )
        )
    return tuple(measured)


def select_by_measure(
    contract: ModelMeasuredSelectionContract,
    candidates: Iterable[ModelSelectionCandidate],
    pricing: Iterable[ModelPricedModel],
    runs: Iterable[ModelRecordedRun],
    *,
    now: datetime,
) -> ModelMeasuredSelection:
    """Rank the contract's candidates under its objective and record the decision.

    Raises:
        MeasureMissingError: a candidate has no measured cost or latency.
    """
    measured = measure_candidates(contract, candidates, pricing, runs, now=now)
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
        decided_at=now,
    )


class HandlerMeasuredModelSelection:
    """Rank on the shipped rules; the decided event carries every measure read.

    The rules are injected at construction and loaded outside this node, so
    ``handle`` reads no file and the request keeps its released wire shape.
    """

    def __init__(
        self, selection_contract: ModelMeasuredSelectionContract | None = None
    ) -> None:
        self._selection_contract = (
            selection_contract
            if selection_contract is not None
            else load_measured_selection_contract()
        )

    def handle(
        self, request: ModelMeasuredSelectionRequest
    ) -> ModelMeasuredSelectionDecidedEvent:
        selection = select_by_measure(
            self._selection_contract,
            request.candidates,
            request.pricing,
            request.runs,
            now=request.decided_at,
        )
        return ModelMeasuredSelectionDecidedEvent(
            correlation_id=request.correlation_id,
            task_type=request.task_type,
            selection=selection,
        )


__all__ = [
    "HandlerMeasuredModelSelection",
    "MeasureMissingError",
    "candidates_from_routing_tiers",
    "measure_candidates",
    "priced_models_from_manifest",
    "recorded_runs_from_rows",
    "select_by_measure",
]
