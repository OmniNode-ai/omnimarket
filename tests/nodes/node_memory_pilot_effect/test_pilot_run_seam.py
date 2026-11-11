# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seam: the daily memory pilot run's result file is what the pilot measurement node holds against the frozen bar (OMN-23619).

Producer: ``HandlerMemoryPilotRun`` writing its result through ``FileResultSink``. Consumer: the real
``HandlerPilotMeasurement`` over ``FileResultSource`` reading that file, with the bar spelled in this test (the
overlays repository's bar for the pilot, metric for metric) and its hash computed by the manifest check's own method.
The test fails when either side renames a metric, changes the file's shape, or stops writing it.

* golden: a candidate within the bar reads WORKING.
* error: a candidate worse than the baseline is rejected (NOT_WORKING); a failed ingest's partial result is rejected for
  its missing metrics; a lab model that is down writes no file and the pilot reads unmeasured (UNKNOWN).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from omnimarket.models.model_reading import EnumReadingVerdict
from omnimarket.models.pilot_manifest.model_pilot_bar_criterion import (
    ModelPilotBarCriterion,
)
from omnimarket.nodes.node_memory_pilot_effect.handlers.handler_memory_pilot_run import (
    HandlerMemoryPilotRun,
)
from omnimarket.nodes.node_memory_pilot_effect.handlers.result_file_sink import (
    FileResultSink,
)
from omnimarket.nodes.node_memory_pilot_effect.models.model_memory_pilot_run import (
    ModelMemoryPilotRunMetricNames,
    ModelMemoryPilotRunRequest,
)
from omnimarket.nodes.node_pilot_manifest_check_compute.handlers.handler_pilot_manifest_check import (
    compute_bar_hash,
)
from omnimarket.nodes.node_pilot_measurement_effect.adapters.file_result_source import (
    FileResultSource,
)
from omnimarket.nodes.node_pilot_measurement_effect.handlers.handler_pilot_measurement import (
    HandlerPilotMeasurement,
)
from omnimarket.nodes.node_pilot_measurement_effect.models.model_pilot_measurement import (
    ModelPilotMeasurementCommand,
    ModelPilotMeasurementResult,
)
from omnimarket.nodes.node_pilot_measurement_effect.overlay import (
    ModelPilotMeasurementOverlay,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    InMemoryLedger,
)
from tests.nodes.node_pilot_measurement_effect.pilot_measurement_support import (
    example_overlay_document,
)

from .conftest import RecordingBus
from .pilot_run_support import (
    CORR,
    NAMES,
    FakeAnswerer,
    FakeAudit,
    FakeCandidate,
    Ticker,
    make_run_overlay,
)

pytestmark = pytest.mark.unit
PILOT = "memory-cognee-stage1"
ENV = "MEMORY_PILOT_RESULT_FILE"
PLANTED_QUESTIONS = 2


def _bar(
    names: ModelPilotMeasurementOverlay | dict[str, str],
) -> list[dict[str, object]]:
    """The pilot's frozen bar, one criterion per measured quantity, thresholds as the overlays repository declares them."""
    return [
        {"metric": NAMES["overall_gain"], "comparator": "ge", "threshold": 0.10},
        {"metric": NAMES["hard_gain"], "comparator": "ge", "threshold": 0.15},
        {
            "metric": NAMES["planted_recalled"],
            "comparator": "ge",
            "threshold": PLANTED_QUESTIONS - 1.0,
        },
        {"metric": NAMES["abstain_margin"], "comparator": "ge", "threshold": 0.0},
        {"metric": NAMES["ingest_seconds"], "comparator": "le", "threshold": 21600.0},
        {"metric": NAMES["failed_items"], "comparator": "le", "threshold": 0.0},
        {"metric": NAMES["query_p95_seconds"], "comparator": "le", "threshold": 60.0},
        {"metric": NAMES["outside_connections"], "comparator": "le", "threshold": 0.0},
    ]


def _measurement_overlay(
    result_file: Path,
) -> tuple[ModelPilotMeasurementOverlay, dict[str, str]]:
    bar = _bar(NAMES)
    document = example_overlay_document()
    document["pilots"] = [
        {
            "pilot": PILOT,
            "bar": bar,
            "bar_hash": compute_bar_hash(
                [ModelPilotBarCriterion.model_validate(c) for c in bar]
            ),
            "frozen_at": "2026-11-05T16:32:00+00:00",
            "started_at": "2026-11-05T17:08:28+00:00",
            "result_file_env": ENV,
            "max_result_age_seconds": 93600,
        }
    ]
    return ModelPilotMeasurementOverlay.model_validate(document), {
        ENV: str(result_file)
    }


async def _produce_then_measure(
    tmp_path: Path, *, candidate: FakeCandidate, baseline_down: bool = False
) -> ModelPilotMeasurementResult:
    overlay = make_run_overlay(tmp_path)
    assert overlay.run is not None
    now = datetime.now(UTC)
    await HandlerMemoryPilotRun(
        event_bus=RecordingBus(),
        overlay=overlay,
        answerer=FakeAnswerer(down=baseline_down),
        result_sink=FileResultSink(overlay.run.result_file),
        connection_audit=FakeAudit(),
        candidate_factory=lambda _overlay, _prompt: candidate,
        clock=lambda: now,
        monotonic=Ticker(),
    ).handle(ModelMemoryPilotRunRequest(correlation_id=CORR))
    measurement, environ = _measurement_overlay(overlay.run.result_file)
    return HandlerPilotMeasurement(
        overlay=measurement,
        host="seam-host",
        results=FileResultSource(environ),
        appender=InMemoryLedger(),
        clock=lambda: now,
    ).handle(ModelPilotMeasurementCommand(correlation_id="seam"))


async def test_golden_a_candidate_within_the_frozen_bar_reads_working(
    tmp_path: Path,
) -> None:
    result = await _produce_then_measure(tmp_path, candidate=FakeCandidate())
    assert result.verdict is EnumReadingVerdict.WORKING, result.reasons


async def test_error_a_candidate_worse_than_the_baseline_is_rejected(
    tmp_path: Path,
) -> None:
    result = await _produce_then_measure(tmp_path, candidate=FakeCandidate(good=False))
    assert result.verdict is EnumReadingVerdict.NOT_WORKING
    assert result.outcomes[0].state.value == "rejected"


async def test_error_a_failed_ingest_is_rejected_for_the_metrics_it_lacks(
    tmp_path: Path,
) -> None:
    result = await _produce_then_measure(
        tmp_path, candidate=FakeCandidate(fail_ingest=True)
    )
    assert result.verdict is EnumReadingVerdict.NOT_WORKING


async def test_error_a_lab_model_that_is_down_leaves_no_result_and_reads_unknown(
    tmp_path: Path,
) -> None:
    result = await _produce_then_measure(
        tmp_path, candidate=FakeCandidate(), baseline_down=True
    )
    assert result.verdict is EnumReadingVerdict.UNKNOWN
    assert result.outcomes[0].state.value == "unmeasured"


def test_the_producers_metric_names_are_the_bars_metric_names() -> None:
    """Renaming a metric on the producer's side (or the bar's) turns this red."""
    declared = set(ModelMemoryPilotRunMetricNames.model_fields)
    assert declared == set(NAMES)
    assert {c["metric"] for c in _bar(NAMES)} == set(NAMES.values())
