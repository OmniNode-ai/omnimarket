# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
# onex-allow-file OMN-10580 reason="test fixture — uses lab LLM endpoint and model ID as test inputs to verify aggregator wiring; not runtime defaults"

"""Unit tests for the ADK-eval measurement harness aggregator (P8)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from experiments.adk_eval.harness.aggregator import aggregate

TOL = 1e-6


def _write_json(path: Path, data: dict[str, Any]) -> Path:
    path.write_text(json.dumps(data))
    return path


def _track_a_metrics() -> dict[str, Any]:
    return {
        "runs": 5,
        "latency_seconds_median": 40.0,
        "latency_seconds_min": 10.0,
        "latency_seconds_max": 50.0,
        "latencies_all": [10.0, 30.0, 40.0, 45.0, 50.0],
        "input_tokens_total": 5000,
        "output_tokens_total": 2500,
        "input_tokens_per_run": [1000, 1000, 1000, 1000, 1000],
        "output_tokens_per_run": [400, 450, 500, 550, 600],
        "llm_calls_per_run": [1, 1, 1, 1, 1],
        "model": "gemini-flash-latest",
        "auth_path": "ai_studio",
        "estimated_cost_usd_per_run_median": 0.008,
    }


def _track_b_metrics() -> dict[str, Any]:
    return {
        "track": "B",
        "runner": "experiments.adk_eval.type_debt_scout_poc",
        "base_url": "http://localhost:8000",
        "model_id": "cyankiwi/Qwen3-Coder-30B-A3B-Instruct-AWQ-4bit",
        "started_at": "2026-04-23T21:00:00+00:00",
        "ended_at": "2026-04-23T21:25:00+00:00",
        "runs_total": 5,
        "runs_ok": 5,
        "runs_failed": 0,
        "latency_median_seconds": 280.0,
        "latency_mean_seconds": 280.0,
        "latency_min_seconds": 260.0,
        "latency_max_seconds": 300.0,
        "per_run": [],
    }


def _scores() -> dict[str, Any]:
    return {
        "track_a": {"binary_f1": 0.95, "macro_f1": 0.54},
        "track_b": {"binary_f1": 0.70, "macro_f1": 0.32},
        "caveats": [],
    }


@pytest.fixture
def inputs(tmp_path: Path) -> tuple[Path, Path, Path]:
    a_path = _write_json(tmp_path / "track_a_metrics.json", _track_a_metrics())
    b_path = _write_json(tmp_path / "track_b_metrics.json", _track_b_metrics())
    scores_path = _write_json(tmp_path / "scores.json", _scores())
    return a_path, b_path, scores_path


@pytest.mark.unit
class TestAggregate:
    def test_emits_generated_at_iso_utc(self, inputs: tuple[Path, Path, Path]) -> None:
        a_path, b_path, scores_path = inputs
        result = aggregate(a_path, b_path, scores_path, 60, 40)
        # ISO 8601, UTC suffix (+00:00 from datetime.isoformat() w/ tz=UTC).
        assert result["generated_at"].endswith("+00:00")

    def test_track_a_block_carries_declared_fields(
        self, inputs: tuple[Path, Path, Path]
    ) -> None:
        a_path, b_path, scores_path = inputs
        result = aggregate(a_path, b_path, scores_path, 60, 40)
        a = result["track_a"]
        assert a["model"] == "gemini-flash-latest"
        assert a["auth_path"] == "ai_studio"
        assert a["latency_seconds_median"] == pytest.approx(40.0, abs=TOL)
        assert a["cost_usd_per_run_median"] == pytest.approx(0.008, abs=TOL)
        assert a["dev_time_minutes_rough"] == 60
        assert a["binary_f1"] == pytest.approx(0.95, abs=TOL)
        assert a["macro_f1"] == pytest.approx(0.54, abs=TOL)
        assert a["tokens_input_median"] == pytest.approx(1000.0, abs=TOL)
        assert a["tokens_output_median"] == pytest.approx(500.0, abs=TOL)

    def test_track_b_block_marks_cost_marginal_zero(
        self, inputs: tuple[Path, Path, Path]
    ) -> None:
        a_path, b_path, scores_path = inputs
        result = aggregate(a_path, b_path, scores_path, 60, 40)
        b = result["track_b"]
        assert b["cost_usd_per_run_median"] == 0.0
        assert "Marginal inference cost only" in b["cost_note"]
        assert "localhost:8000" in b["model"]
        assert b["latency_seconds_median"] == pytest.approx(280.0, abs=TOL)
        assert b["dev_time_minutes_rough"] == 40
        assert b["binary_f1"] == pytest.approx(0.70, abs=TOL)
        assert b["macro_f1"] == pytest.approx(0.32, abs=TOL)

    def test_ratios_reflect_b_slower_and_f1_gap_pp(
        self, inputs: tuple[Path, Path, Path]
    ) -> None:
        a_path, b_path, scores_path = inputs
        result = aggregate(a_path, b_path, scores_path, 60, 40)
        ratios = result["ratios"]
        assert ratios["latency_b_over_a"] == pytest.approx(7.0, abs=TOL)
        assert ratios["dev_time_a_over_b"] == pytest.approx(1.5, abs=TOL)
        # |0.95 - 0.70| * 100 = 25.0 pp
        assert ratios["binary_f1_gap_pp"] == pytest.approx(25.0, abs=TOL)
        assert "functionally infinite" in ratios["cost_a_over_b_marginal_note"]

    def test_caveats_cover_dev_time_cost_and_sample_size(
        self, inputs: tuple[Path, Path, Path]
    ) -> None:
        a_path, b_path, scores_path = inputs
        result = aggregate(a_path, b_path, scores_path, 60, 40)
        caveats = result["caveats"]
        assert len(caveats) == 3
        assert any("Dev-time is rough" in c for c in caveats)
        assert any("Marginal" in c or "marginal" in c for c in caveats)
        assert any("LLM-self-labeled" in c for c in caveats)

    def test_median_tokens_handles_even_count(self, tmp_path: Path) -> None:
        a = _track_a_metrics()
        a["input_tokens_per_run"] = [100, 200, 300, 400]
        a["output_tokens_per_run"] = [10, 20, 30, 40]
        a_path = _write_json(tmp_path / "a.json", a)
        b_path = _write_json(tmp_path / "b.json", _track_b_metrics())
        scores_path = _write_json(tmp_path / "s.json", _scores())
        result = aggregate(a_path, b_path, scores_path, 60, 40)
        # even-count median of [100,200,300,400] → 250
        assert result["track_a"]["tokens_input_median"] == pytest.approx(250.0, abs=TOL)
        # even-count median of [10,20,30,40] → 25
        assert result["track_a"]["tokens_output_median"] == pytest.approx(25.0, abs=TOL)

    def test_zero_dev_time_b_does_not_explode(
        self, inputs: tuple[Path, Path, Path]
    ) -> None:
        a_path, b_path, scores_path = inputs
        # dev_time_b=0 is an absurd input, but the aggregator should
        # degrade gracefully rather than raise — the output still has to
        # emit for the P10 write-up.
        result = aggregate(a_path, b_path, scores_path, 60, 0)
        assert result["ratios"]["dev_time_a_over_b"] == 0.0
