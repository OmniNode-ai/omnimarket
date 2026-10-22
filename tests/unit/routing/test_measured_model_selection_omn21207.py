# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for measured model selection (OMN-21207)."""

from __future__ import annotations

import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_infra.models.pricing.model_pricing_table import ModelPricingTable

from omnimarket.models.delegation.wire.model_routing_decision import (
    ModelRoutingDecision,
)
from omnimarket.routing.measured_model_selection import (
    CONTRACT_PATH,
    MeasuredSelectionContractError,
    MeasureMissingError,
    ModelMeasuredSelection,
    ModelMeasuredSelectionContract,
    ModelRecordedRun,
    _read_contract,
    load_measured_selection_contract,
    read_recorded_runs,
    select_by_measure,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 10, 22, 12, tzinfo=UTC)


def _contract(kind: str = "cheapest_acceptable") -> ModelMeasuredSelectionContract:
    return ModelMeasuredSelectionContract.model_validate(
        {
            "schema_version": "measured_model_selection.v1",
            "objective": {"kind": kind, "basis": "note"},
            "input_share": 0.75,
            "latency": {"window_days": 14, "min_samples": 3},
            "candidates": [
                {
                    "model_id": "cheap",
                    "backend_ref": "b-cheap",
                    "measured_cost_per_1m": {
                        "source": "pricing_manifest",
                        "pricing_key": "cheap",
                    },
                    "measured_p50_latency": {
                        "source": "delegation_runs",
                        "backend_id": "b-cheap",
                        "model_name": "cheap",
                    },
                },
                {
                    "model_id": "dear",
                    "backend_ref": "b-dear",
                    "measured_cost_per_1m": {
                        "source": "pricing_manifest",
                        "pricing_key": "dear",
                    },
                    "measured_p50_latency": {
                        "source": "delegation_runs",
                        "backend_id": "b-dear",
                        "model_name": "dear",
                    },
                },
            ],
        }
    )


def _pricing(include_dear: bool = True) -> ModelPricingTable:
    models: dict[str, Any] = {
        "cheap": {
            "input_cost_per_1k": 0.0001,
            "output_cost_per_1k": 0.0004,
            "effective_date": "2026-10-06",
        }
    }
    if include_dear:
        models["dear"] = {
            "input_cost_per_1k": 0.003,
            "output_cost_per_1k": 0.015,
            "effective_date": "2026-10-01",
        }
    return ModelPricingTable.from_dict({"schema_version": "1.0.0", "models": models})


def _run(
    backend_id: str,
    model_name: str,
    latency_ms: int,
    recorded_at: datetime,
) -> ModelRecordedRun:
    return ModelRecordedRun(
        backend_id=backend_id,
        model_name=model_name,
        latency_ms=latency_ms,
        recorded_at=recorded_at,
    )


def _standard_runs() -> tuple[ModelRecordedRun, ...]:
    cheap = tuple(
        _run("b-cheap", "cheap", 900, NOW - timedelta(hours=hour)) for hour in (3, 2, 1)
    )
    dear = tuple(
        _run("b-dear", "dear", 100, NOW - timedelta(hours=hour)) for hour in (3, 2, 1)
    )
    return cheap + dear


def _shipped_contract_raw() -> dict[str, Any]:
    return yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))


def _write_contract(raw: dict[str, Any], tmp_path: Path) -> Path:
    path = tmp_path / "contract.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return path


def _routing_decision(
    measured_selection: ModelMeasuredSelection | None,
) -> ModelRoutingDecision:
    return ModelRoutingDecision(
        correlation_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
        task_type="test",
        selected_model="cheap",
        selected_backend_id=uuid.UUID("00000000-0000-0000-0000-000000000002"),
        endpoint_url="https://example.com/v1",
        cost_tier="cheap",
        max_context_tokens=128_000,
        max_tokens=4_096,
        system_prompt="test",
        rationale="test",
        measured_selection=measured_selection,
    )


def test_ac1_every_shipped_candidate_declares_a_measured_cost_and_latency_source() -> (
    None
):
    contract = load_measured_selection_contract()
    for candidate in contract.candidates:
        assert candidate.measured_cost_per_1m.source == "pricing_manifest"
        assert candidate.measured_p50_latency.source == "delegation_runs"


def test_ac1_every_shipped_candidate_is_priced_by_the_manifest() -> None:
    contract = load_measured_selection_contract()
    pricing = ModelPricingTable.from_yaml()
    for candidate in contract.candidates:
        assert pricing.has_model(candidate.measured_cost_per_1m.pricing_key)


def test_ac1_a_candidate_without_a_cost_source_is_refused(tmp_path: Path) -> None:
    raw = _shipped_contract_raw()
    del raw["candidates"][0]["measured_cost_per_1m"]
    path = _write_contract(raw, tmp_path)
    with pytest.raises(MeasuredSelectionContractError):
        _read_contract(path)


def test_ac1_a_candidate_without_a_latency_source_is_refused(tmp_path: Path) -> None:
    raw = _shipped_contract_raw()
    del raw["candidates"][0]["measured_p50_latency"]
    path = _write_contract(raw, tmp_path)
    with pytest.raises(MeasuredSelectionContractError):
        _read_contract(path)


def test_ac1_a_hardcoded_value_is_refused(tmp_path: Path) -> None:
    raw = _shipped_contract_raw()
    raw["candidates"][0]["measured_cost_per_1m"]["value"] = 1.0
    path = _write_contract(raw, tmp_path)
    with pytest.raises(MeasuredSelectionContractError):
        _read_contract(path)


def test_ac2_cheapest_acceptable_ranks_on_cost() -> None:
    selection = select_by_measure(
        _contract(),
        _pricing(),
        _standard_runs(),
        now=NOW,
    )
    assert selection.selected_model == "cheap"
    assert [measure.model_id for measure in selection.ranked] == ["cheap", "dear"]


def test_ac2_fastest_acceptable_ranks_on_latency() -> None:
    selection = select_by_measure(
        _contract("fastest_acceptable"),
        _pricing(),
        _standard_runs(),
        now=NOW,
    )
    assert selection.selected_model == "dear"
    assert [measure.model_id for measure in selection.ranked] == ["dear", "cheap"]


def test_ac2_missing_latency_raises() -> None:
    cheap = tuple(
        _run("b-cheap", "cheap", 900, NOW - timedelta(hours=hour)) for hour in (3, 2, 1)
    )
    dear = tuple(
        _run("b-dear", "dear", 100, NOW - timedelta(hours=hour)) for hour in (1, 2)
    )
    with pytest.raises(MeasureMissingError) as exc_info:
        select_by_measure(_contract(), _pricing(), cheap + dear, now=NOW)
    assert exc_info.value.candidate == "dear"
    assert exc_info.value.measure == "p50_latency"


def test_ac2_missing_cost_raises() -> None:
    with pytest.raises(MeasureMissingError) as exc_info:
        select_by_measure(
            _contract(),
            _pricing(include_dear=False),
            _standard_runs(),
            now=NOW,
        )
    assert exc_info.value.candidate == "dear"
    assert exc_info.value.measure == "cost_per_1m"


def test_ac2_runs_outside_the_window_do_not_count() -> None:
    cheap = tuple(
        _run("b-cheap", "cheap", 900, NOW - timedelta(hours=hour)) for hour in (3, 2, 1)
    )
    dear = tuple(
        _run("b-dear", "dear", 100, NOW - timedelta(days=15)) for _ in range(3)
    )
    with pytest.raises(MeasureMissingError) as exc_info:
        select_by_measure(_contract(), _pricing(), cheap + dear, now=NOW)
    assert exc_info.value.candidate == "dear"
    assert exc_info.value.measure == "p50_latency"


def test_ac3_the_decision_records_measure_values_and_captured_at() -> None:
    selection = select_by_measure(
        _contract(),
        _pricing(),
        _standard_runs(),
        now=NOW,
    )
    cheap = next(measure for measure in selection.ranked if measure.model_id == "cheap")
    assert cheap.cost_per_1m_usd == pytest.approx(0.175)
    assert cheap.p50_latency_ms == pytest.approx(900)
    assert cheap.latency_sample_count == 3
    assert cheap.cost_captured_at == datetime(2026, 10, 6, tzinfo=UTC)
    assert cheap.latency_captured_at == NOW - timedelta(hours=1)


def test_ac3_the_routing_decision_event_carries_the_measures() -> None:
    selection = select_by_measure(
        _contract(),
        _pricing(),
        _standard_runs(),
        now=NOW,
    )
    decision = _routing_decision(selection)
    payload = decision.model_dump(mode="json")
    assert payload["measured_selection"]["ranked"][0]["p50_latency_ms"] == 900
    ranked_entry = payload["measured_selection"]["ranked"][0]
    assert "cost_captured_at" in ranked_entry
    assert "latency_captured_at" in ranked_entry
    assert ModelRoutingDecision.model_validate(payload) == decision


def test_ac3_a_decision_without_measures_omits_the_field() -> None:
    decision = _routing_decision(None)
    payload = decision.model_dump()
    assert "measured_selection" not in payload


def test_read_recorded_runs_reads_terminal_ok_rows(tmp_path: Path) -> None:
    db_path = tmp_path / "delegation_events.db"
    conn = sqlite3.connect(db_path)
    try:
        with conn:
            conn.execute(
                "CREATE TABLE delegation_events ("
                "backend_id TEXT, model_name TEXT, delegation_latency_ms INTEGER, "
                "latency_ms INTEGER, timestamp TEXT, terminal_ok INTEGER)"
            )
            ok_cheap_ts = (NOW - timedelta(days=1)).isoformat()
            ok_dear_ts = (NOW - timedelta(days=2)).isoformat()
            not_terminal_ts = (NOW - timedelta(days=3)).isoformat()
            null_latency_ts = (NOW - timedelta(days=4)).isoformat()
            conn.executemany(
                "INSERT INTO delegation_events ("
                "backend_id, model_name, delegation_latency_ms, "
                "latency_ms, timestamp, terminal_ok"
                ") VALUES (?, ?, ?, ?, ?, ?)",
                [
                    ("b-cheap", "cheap", 100, None, ok_cheap_ts, 1),
                    ("b-dear", "dear", None, 200, ok_dear_ts, 1),
                    ("b-cheap", "cheap", 300, None, not_terminal_ts, 0),
                    ("b-cheap", "cheap", None, None, null_latency_ts, 1),
                ],
            )
    finally:
        conn.close()

    runs = read_recorded_runs(db_path, since=NOW - timedelta(days=14))
    assert len(runs) == 2
    assert {(run.backend_id, run.model_name, run.latency_ms) for run in runs} == {
        ("b-cheap", "cheap", 100),
        ("b-dear", "dear", 200),
    }
    assert {run.recorded_at for run in runs} == {
        NOW - timedelta(days=1),
        NOW - timedelta(days=2),
    }


def test_ac4_the_contract_names_its_objective_and_the_note_that_approves_it() -> None:
    objective = load_measured_selection_contract().objective
    assert objective.kind == "cheapest_acceptable"
    assert objective.basis == (
        "knowledge-base-internal "
        "reference/decisions/delegation-measured-selection-objective.md"
    )
