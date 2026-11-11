# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The file result source reads a good file and raises PilotResultError for every bad one."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnimarket.nodes.node_pilot_measurement_effect.adapters.file_result_source import (
    FileResultSource,
)
from omnimarket.nodes.node_pilot_measurement_effect.overlay import ModelPilotDeclaration
from omnimarket.nodes.node_pilot_measurement_effect.protocols import PilotResultError

pytestmark = pytest.mark.unit


def _declaration(env: str = "EXAMPLE_RESULT") -> ModelPilotDeclaration:
    return ModelPilotDeclaration(
        pilot="example-pilot",
        bar=(),
        bar_hash=None,
        frozen_at=None,
        started_at="2025-01-01T00:00:00+00:00",
        result_file_env=env,
        max_result_age_seconds=3600,
    )


def test_a_good_file_reads_its_metrics_and_time(tmp_path: Path) -> None:
    path = tmp_path / "result.json"
    path.write_text(
        json.dumps(
            {
                "measured_at": "2026-01-15T12:00:00+00:00",
                "metrics": {"completeness": 0.95, "regressions": 0.0},
            }
        ),
        encoding="utf-8",
    )
    source = FileResultSource({"EXAMPLE_RESULT": str(path)})
    read = source.read(_declaration())
    assert read.metrics == {"completeness": 0.95, "regressions": 0.0}
    assert read.measured_at.isoformat() == "2026-01-15T12:00:00+00:00"


def test_a_missing_file_raises_pilot_result_error(tmp_path: Path) -> None:
    source = FileResultSource({"EXAMPLE_RESULT": str(tmp_path / "absent.json")})
    with pytest.raises(PilotResultError):
        source.read(_declaration())


def test_an_unset_variable_raises_pilot_result_error() -> None:
    source = FileResultSource({})
    with pytest.raises(PilotResultError):
        source.read(_declaration())


def test_bad_json_raises_pilot_result_error(tmp_path: Path) -> None:
    path = tmp_path / "result.json"
    path.write_text("not json", encoding="utf-8")
    source = FileResultSource({"EXAMPLE_RESULT": str(path)})
    with pytest.raises(PilotResultError):
        source.read(_declaration())


def test_a_metric_that_is_not_a_number_raises_pilot_result_error(
    tmp_path: Path,
) -> None:
    path = tmp_path / "result.json"
    path.write_text(
        json.dumps(
            {
                "measured_at": "2026-01-15T12:00:00+00:00",
                "metrics": {"completeness": "not-a-number"},
            }
        ),
        encoding="utf-8",
    )
    source = FileResultSource({"EXAMPLE_RESULT": str(path)})
    with pytest.raises(PilotResultError):
        source.read(_declaration())


def test_a_measured_at_without_a_time_zone_raises_pilot_result_error(
    tmp_path: Path,
) -> None:
    path = tmp_path / "result.json"
    path.write_text(
        json.dumps(
            {
                "measured_at": "2026-01-15T12:00:00",
                "metrics": {"completeness": 0.95},
            }
        ),
        encoding="utf-8",
    )
    source = FileResultSource({"EXAMPLE_RESULT": str(path)})
    with pytest.raises(PilotResultError):
        source.read(_declaration())
