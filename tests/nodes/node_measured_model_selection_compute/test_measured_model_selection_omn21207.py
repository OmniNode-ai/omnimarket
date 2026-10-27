# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-21207: delegation ranks candidates on measured cost and latency.

AC1: every candidate, taken from the routing contract's tiers, carries where
its cost per 1M tokens (the pricing manifest) and its p50 latency (recorded
delegation runs) are read, never a value. AC2: the selector ranks under one declared objective and raises on a
missing measure instead of falling back to the declared order. AC3: the decided
event carries every measure value and its captured_at. AC4: the contract names
its objective and the ticket whose record quotes the operator approving it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.handlers.handler_measured_selection_config import (
    CONTRACT_PATH,
    MeasuredSelectionContractError,
    load_measured_selection_contract,
    read_selection_contract,
)
from omnimarket.models.delegation.model_measured_selection import (
    ModelMeasuredSelectionContract,
    ModelPricedModel,
    ModelRecordedRun,
    ModelSelectionCandidate,
)
from omnimarket.models.delegation.wire.model_measured_selection_decided_event import (
    ModelMeasuredSelectionDecidedEvent,
    ModelMeasuredSelectionRequest,
)
from omnimarket.nodes.node_measured_model_selection_compute.handlers.handler_measured_model_selection import (
    HandlerMeasuredModelSelection,
    MeasureMissingError,
    candidates_from_routing_tiers,
    recorded_runs_from_rows,
    select_by_measure,
)
from omnimarket.routing.routing_tiers_path import resolve_routing_tiers_path


def routing_contract_candidates() -> tuple[ModelSelectionCandidate, ...]:
    """The shipped rule applied to the routing contract in force."""
    routing_tiers: Any = yaml.safe_load(
        resolve_routing_tiers_path().read_text(encoding="utf-8")
    )
    return candidates_from_routing_tiers(
        load_measured_selection_contract().candidates, routing_tiers
    )


pytestmark = pytest.mark.unit

NOW = datetime(2026, 10, 22, 12, tzinfo=UTC)


def _candidate(name: str) -> dict[str, Any]:
    return {
        "model_id": name,
        "backend_ref": f"b-{name}",
        "measured_cost_per_1m": {"source": "pricing_manifest", "pricing_key": name},
        "measured_p50_latency": {
            "source": "delegation_runs",
            "backend_id": f"b-{name}",
            "model_name": name,
        },
    }


def _contract(kind: str = "cheapest_acceptable") -> ModelMeasuredSelectionContract:
    return ModelMeasuredSelectionContract.model_validate(
        {
            "objective": {"kind": kind, "basis": "OMN-21207"},
            "input_share": 0.75,
            "latency": {"window_days": 14, "min_samples": 3},
            "candidates": {
                "source": "routing_tiers",
                "tiers": ["t"],
                "measured_cost_per_1m": "pricing_manifest",
                "measured_p50_latency": "delegation_runs",
            },
        }
    )


CANDIDATES = tuple(
    ModelSelectionCandidate.model_validate(_candidate(name))
    for name in ("cheap", "dear")
)


def _pricing(*, include_dear: bool = True) -> tuple[ModelPricedModel, ...]:
    prices = [
        ModelPricedModel(
            pricing_key="cheap",
            input_cost_per_1k=0.0001,
            output_cost_per_1k=0.0004,
            captured_at=datetime(2026, 10, 6, tzinfo=UTC),
        )
    ]
    if include_dear:
        prices.append(
            ModelPricedModel(
                pricing_key="dear",
                input_cost_per_1k=0.003,
                output_cost_per_1k=0.015,
                captured_at=datetime(2026, 10, 1, tzinfo=UTC),
            )
        )
    return tuple(prices)


def _runs(
    name: str, latency_ms: int, hours_ago: tuple[int, ...]
) -> list[ModelRecordedRun]:
    return [
        ModelRecordedRun(
            backend_id=f"b-{name}",
            model_name=name,
            latency_ms=latency_ms,
            recorded_at=NOW - timedelta(hours=hours),
        )
        for hours in hours_ago
    ]


def _standard_runs() -> list[ModelRecordedRun]:
    return _runs("cheap", 900, (1, 2, 3)) + _runs("dear", 100, (1, 2, 3))


def test_ac1_every_routing_contract_candidate_carries_a_cost_and_latency_source() -> (
    None
):
    routing_tiers = yaml.safe_load(resolve_routing_tiers_path().read_text("utf-8"))
    named = set(load_measured_selection_contract().candidates.tiers)
    expected = {
        (str(model["id"]), str(model["backend_id"]))
        for tier in routing_tiers["tiers"]
        if tier["name"] in named
        for model in tier["models"]
    }
    candidates = routing_contract_candidates()
    assert {(c.model_id, c.backend_ref) for c in candidates} == expected
    for candidate in candidates:
        assert candidate.measured_cost_per_1m.source == "pricing_manifest"
        assert candidate.measured_cost_per_1m.pricing_key == candidate.model_id
        assert candidate.measured_p50_latency.source == "delegation_runs"
        assert candidate.measured_p50_latency.backend_id == candidate.backend_ref
        assert candidate.measured_p50_latency.model_name == candidate.model_id


@pytest.mark.parametrize("field", ["measured_cost_per_1m", "measured_p50_latency"])
def test_ac1_a_candidate_without_a_measure_source_is_refused(field: str) -> None:
    raw = _candidate("cheap")
    raw.pop(field)
    with pytest.raises(ValidationError):
        ModelSelectionCandidate.model_validate(raw)


def test_ac1_a_hardcoded_value_is_refused() -> None:
    raw = _candidate("cheap")
    raw["measured_cost_per_1m"]["value"] = 1.0
    with pytest.raises(ValidationError):
        ModelSelectionCandidate.model_validate(raw)


@pytest.mark.parametrize("field", ["measured_cost_per_1m", "measured_p50_latency"])
def test_ac1_a_contract_rule_without_a_measure_source_is_refused(
    tmp_path: Path, field: str
) -> None:
    raw = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))
    raw["measured_selection"]["candidates"].pop(field)
    path = tmp_path / "contract.yaml"
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    with pytest.raises(MeasuredSelectionContractError):
        read_selection_contract(path)


def test_ac1_a_tier_the_routing_contract_lacks_is_refused() -> None:
    with pytest.raises(MeasuredSelectionContractError):
        candidates_from_routing_tiers(
            _contract().candidates, {"tiers": [{"name": "other", "models": []}]}
        )


def test_ac2_cheapest_acceptable_ranks_on_cost() -> None:
    selection = select_by_measure(
        _contract(), CANDIDATES, _pricing(), _standard_runs(), now=NOW
    )
    assert selection.selected_model == "cheap"
    assert [m.model_id for m in selection.ranked] == ["cheap", "dear"]


def test_ac2_fastest_acceptable_ranks_on_latency() -> None:
    selection = select_by_measure(
        _contract("fastest_acceptable"),
        CANDIDATES,
        _pricing(),
        _standard_runs(),
        now=NOW,
    )
    assert selection.selected_model == "dear"
    assert [m.model_id for m in selection.ranked] == ["dear", "cheap"]


def test_ac2_missing_latency_raises() -> None:
    runs = _runs("cheap", 900, (1, 2, 3)) + _runs("dear", 100, (1, 2))
    with pytest.raises(MeasureMissingError) as exc_info:
        select_by_measure(_contract(), CANDIDATES, _pricing(), runs, now=NOW)
    assert exc_info.value.candidate == "dear"
    assert exc_info.value.measure == "p50_latency"


def test_ac2_missing_cost_raises() -> None:
    with pytest.raises(MeasureMissingError) as exc_info:
        select_by_measure(
            _contract(),
            CANDIDATES,
            _pricing(include_dear=False),
            _standard_runs(),
            now=NOW,
        )
    assert exc_info.value.candidate == "dear"
    assert exc_info.value.measure == "cost_per_1m"


def test_ac2_runs_outside_the_window_do_not_count() -> None:
    runs = _runs("cheap", 900, (1, 2, 3)) + _runs("dear", 100, (15 * 24,) * 3)
    with pytest.raises(MeasureMissingError) as exc_info:
        select_by_measure(_contract(), CANDIDATES, _pricing(), runs, now=NOW)
    assert exc_info.value.candidate == "dear"


def test_ac3_the_decision_records_measure_values_and_captured_at() -> None:
    selection = select_by_measure(
        _contract(), CANDIDATES, _pricing(), _standard_runs(), now=NOW
    )
    cheap = selection.ranked[0]
    assert cheap.cost_per_1m_usd == pytest.approx(
        1000 * (0.0001 * 0.75 + 0.0004 * 0.25)
    )
    assert cheap.cost_source == "pricing_manifest:cheap"
    assert cheap.cost_captured_at == datetime(2026, 10, 6, tzinfo=UTC)
    assert cheap.p50_latency_ms == 900
    assert cheap.latency_sample_count == 3
    assert cheap.latency_captured_at == NOW - timedelta(hours=1)
    assert selection.decided_at == NOW


def test_ac3_the_decided_event_carries_the_measures_and_round_trips(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidates = routing_contract_candidates()
    pricing = tuple(
        ModelPricedModel(
            pricing_key=key,
            input_cost_per_1k=0.001 * (index + 1),
            output_cost_per_1k=0.002 * (index + 1),
            captured_at=datetime(2026, 10, 6, tzinfo=UTC),
        )
        for index, key in enumerate(
            dict.fromkeys(c.measured_cost_per_1m.pricing_key for c in candidates)
        )
    )
    runs = tuple(
        ModelRecordedRun(
            backend_id=c.measured_p50_latency.backend_id,
            model_name=c.measured_p50_latency.model_name,
            latency_ms=200 + hours,
            recorded_at=NOW - timedelta(hours=hours),
        )
        for c in candidates
        for hours in (1, 2, 3)
    )
    request = ModelMeasuredSelectionRequest(
        correlation_id=uuid4(),
        task_type="code_generation",
        candidates=candidates,
        pricing=pricing,
        runs=runs,
        decided_at=NOW,
    )
    handler = HandlerMeasuredModelSelection(load_measured_selection_contract())

    def forbid_file_read(*args: object, **kwargs: object) -> str:
        raise AssertionError("COMPUTE attempted a file read")

    with monkeypatch.context() as guard:
        guard.setattr(Path, "read_text", forbid_file_read)
        event = handler.handle(request)

    assert event.task_type == request.task_type
    assert event.selection.selected_model == "Qwen3.8-27B"
    payload = event.model_dump(mode="json")
    assert payload["correlation_id"] == str(request.correlation_id)
    ranked = payload["selection"]["ranked"]
    assert len(ranked) == len(candidates)
    for entry in ranked:
        assert entry["p50_latency_ms"] == 202
        assert entry["cost_captured_at"] == "2026-10-06T00:00:00Z"
        assert entry["latency_captured_at"] == "2026-10-22T11:00:00Z"
    assert ModelMeasuredSelectionDecidedEvent.model_validate(payload) == event


def test_ac3_the_node_publishes_the_decided_event_as_its_terminal() -> None:
    contract = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))
    topic = "onex.evt.omnimarket.measured-model-selection-decided.v1"
    assert contract["terminal_event"] == topic
    assert contract["event_bus"]["publish_topics"] == [topic]


def test_ac3_recorded_runs_are_read_from_terminal_ok_delegation_rows() -> None:
    stamp = (NOW - timedelta(hours=1)).isoformat()
    rows = [
        {"backend_id": "b", "model_name": "m", "delegation_latency_ms": 120,
         "latency_ms": None, "timestamp": stamp, "terminal_ok": 1},
        {"backend_id": "b", "model_name": "m", "delegation_latency_ms": None,
         "latency_ms": 80, "timestamp": stamp, "terminal_ok": 1},
        {"backend_id": "b", "model_name": "m", "delegation_latency_ms": 50,
         "latency_ms": None, "timestamp": stamp, "terminal_ok": 0},
        {"backend_id": "b", "model_name": "m", "delegation_latency_ms": None,
         "latency_ms": None, "timestamp": stamp, "terminal_ok": 1},
    ]  # fmt: skip
    runs = recorded_runs_from_rows(rows)
    assert [run.latency_ms for run in runs] == [120, 80]
    assert {run.recorded_at for run in runs} == {NOW - timedelta(hours=1)}


def test_ac4_the_contract_names_its_objective_and_the_ticket_that_approves_it() -> None:
    objective = load_measured_selection_contract().objective
    assert objective.kind == "cheapest_acceptable"
    assert objective.basis == "OMN-21207"
