# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC1 of OMN-21114 (TA.12): the compaction build latency qualification table."""

from __future__ import annotations

import math
from uuid import UUID

import pytest
from pydantic import ValidationError

from omnimarket.models.context_compaction import ModelContextCompactionPrepared
from omnimarket.nodes.node_context_compaction_latency_compute.handlers import (
    HandlerLatencyQualification,
)
from omnimarket.nodes.node_context_compaction_latency_compute.models import (
    ModelLatencyCell,
    ModelLatencyQualificationRequest,
    ModelLatencyQualificationResult,
)
from omnimarket.nodes.node_context_compaction_latency_compute.models.model_latency_qualification import (
    EnumRoundTripTemperature,
    ModelCompactionRoundTrip,
)

pytestmark = pytest.mark.unit

SIZES = ("smallest", "median", "largest")
HOSTS = ("launching-mac", "lab-host")
BASE = {"smallest": 100, "median": 200, "largest": 400}


def _trips(
    *,
    sizes: tuple[str, ...] = SIZES,
    hosts: tuple[str, ...] = HOSTS,
    count: int = 20,
) -> list[dict[str, object]]:
    """count cold and count warm round trips per size and host; durations 1..count steps."""
    rows: list[dict[str, object]] = []
    for size in sizes:
        for host in hosts:
            for temperature in ("cold", "warm"):
                for i in range(1, count + 1):
                    scale = 3 if temperature == "cold" else 1
                    rows.append(
                        {
                            "round_trip_id": f"{size}-{host}-{temperature}-{i}",
                            "size": size,
                            "host": host,
                            "temperature": temperature,
                            "deterministic_build_ms": BASE[size] * scale + i,
                            "model_section_ms": BASE[size] * 10 * scale + i * 10,
                        }
                    )
    return rows


def _request(**overrides: object) -> ModelLatencyQualificationRequest:
    body: dict[str, object] = {
        "round_trips": _trips(),
        "required_sizes": list(SIZES),
        "required_hosts": list(HOSTS),
        "margin_ms": 2000,
    }
    body.update(overrides)
    return ModelLatencyQualificationRequest.model_validate(body)


def _cell(
    result: ModelLatencyQualificationResult, size: str, host: str, temperature: str
) -> ModelLatencyCell:
    return next(
        c
        for c in result.cells
        if (c.size, c.host, c.temperature.value) == (size, host, temperature)
    )


def test_percentiles_use_nearest_rank_per_size_host_and_phase() -> None:
    result = HandlerLatencyQualification().handle(_request())
    cell = _cell(result, "median", "lab-host", "warm")
    # deterministic build durations are 201..220: nearest rank p50 = 10th, p95 = 19th
    assert cell.count == 20
    assert (cell.deterministic_build.p50_ms, cell.deterministic_build.p95_ms) == (
        210,
        219,
    )
    assert cell.deterministic_build.max_ms == 220
    # model section durations are 2010..2200 in steps of 10
    assert (cell.model_section.p50_ms, cell.model_section.p95_ms) == (2100, 2190)
    assert cell.model_section.max_ms == 2200


def test_one_cell_per_size_host_and_temperature() -> None:
    result = HandlerLatencyQualification().handle(_request())
    assert len(result.cells) == len(SIZES) * len(HOSTS) * 2
    assert result.complete
    assert result.falsifiers == ()


def test_total_is_summed_per_round_trip_not_per_percentile() -> None:
    spiky = [
        {
            "round_trip_id": f"t{i}",
            "size": "smallest",
            "host": "lab-host",
            "temperature": "warm",
            "deterministic_build_ms": 1000 if i == 1 else 1,
            "model_section_ms": 1 if i == 1 else 1000,
        }
        for i in range(1, 21)
    ]
    trips = [
        t
        for t in _trips()
        if (t["size"], t["host"], t["temperature"]) != ("smallest", "lab-host", "warm")
    ]
    result = HandlerLatencyQualification().handle(
        _request(round_trips=[*trips, *spiky])
    )
    cell = _cell(result, "smallest", "lab-host", "warm")
    assert cell.deterministic_build.p95_ms == 1
    assert cell.model_section.p95_ms == 1000
    assert cell.total.max_ms == 1001
    assert cell.total.p95_ms == 1001  # the 19th of 20 sorted totals


def test_proposals_are_not_below_the_measured_p95() -> None:
    result = HandlerLatencyQualification().handle(_request())
    worst_total_p95 = max(c.total.p95_ms for c in result.cells)
    assert result.build_p95_ms == worst_total_p95
    assert result.class_ceiling_ms == worst_total_p95 + 2000
    assert result.precompute_deadline_ms == worst_total_p95 + 5000
    assert result.class_ceiling_ms >= result.build_p95_ms
    assert result.precompute_deadline_ms >= result.build_p95_ms


def test_precompute_slack_is_a_request_field_defaulting_to_five_seconds() -> None:
    result = HandlerLatencyQualification().handle(_request(precompute_slack_ms=7000))
    assert result.precompute_deadline_ms == result.build_p95_ms + 7000


@pytest.mark.parametrize("missing", SIZES)
def test_a_missing_size_is_a_falsifier_and_withholds_proposals(missing: str) -> None:
    kept = tuple(s for s in SIZES if s != missing)
    result = HandlerLatencyQualification().handle(
        _request(round_trips=_trips(sizes=kept))
    )
    assert not result.complete
    assert any(f"size {missing}" in f for f in result.falsifiers)
    assert result.class_ceiling_ms is None
    assert result.precompute_deadline_ms is None


def test_a_missing_host_is_a_falsifier_and_withholds_proposals() -> None:
    result = HandlerLatencyQualification().handle(
        _request(round_trips=_trips(hosts=("lab-host",)))
    )
    assert not result.complete
    assert any("host launching-mac" in f for f in result.falsifiers)
    assert result.precompute_deadline_ms is None


def test_too_few_warm_round_trips_in_one_cell_is_a_falsifier() -> None:
    trips = [
        t
        for t in _trips()
        if not (
            t["size"] == "largest"
            and t["host"] == "lab-host"
            and t["temperature"] == "warm"
            and str(t["round_trip_id"]).endswith(("-1", "-2"))
        )
    ]
    result = HandlerLatencyQualification().handle(_request(round_trips=trips))
    assert not result.complete
    assert any(
        "largest" in f and "lab-host" in f and "warm" in f and "18 of 20" in f
        for f in result.falsifiers
    )
    assert result.class_ceiling_ms is None


def test_a_duplicate_round_trip_id_is_refused_so_the_count_cannot_be_padded() -> None:
    trips = _trips()
    trips.append(dict(trips[0]))
    with pytest.raises(ValidationError, match="duplicate round_trip_id"):
        _request(round_trips=trips)


def test_a_candidate_deadline_below_the_measured_p95_is_a_falsifier() -> None:
    probe = HandlerLatencyQualification().handle(_request())
    result = HandlerLatencyQualification().handle(
        _request(
            candidate_class_ceiling_ms=probe.build_p95_ms - 1,
            candidate_precompute_deadline_ms=probe.build_p95_ms - 1,
        )
    )
    assert not result.complete or result.falsifiers
    assert any("class ceiling" in f for f in result.falsifiers)
    assert any("precompute deadline" in f for f in result.falsifiers)


def test_candidate_deadlines_at_or_above_the_p95_pass() -> None:
    probe = HandlerLatencyQualification().handle(_request())
    result = HandlerLatencyQualification().handle(
        _request(
            candidate_class_ceiling_ms=probe.build_p95_ms,
            candidate_precompute_deadline_ms=probe.build_p95_ms + 5000,
        )
    )
    assert result.falsifiers == ()


def test_a_candidate_cannot_be_checked_against_an_incomplete_table() -> None:
    result = HandlerLatencyQualification().handle(
        _request(
            round_trips=_trips(sizes=("smallest", "median")),
            candidate_class_ceiling_ms=10**9,
        )
    )
    assert any("candidate class ceiling unchecked" in f for f in result.falsifiers)


@pytest.mark.parametrize("bad", [-1.0, math.nan, math.inf])
def test_negative_and_non_finite_durations_are_refused(bad: float) -> None:
    trips = _trips()
    trips[0]["model_section_ms"] = bad
    with pytest.raises(ValidationError):
        _request(round_trips=trips)


def test_fewer_than_twenty_required_samples_cannot_be_configured() -> None:
    with pytest.raises(ValidationError):
        _request(min_samples_per_cell=19)


def test_one_required_host_is_refused_because_the_plan_needs_two() -> None:
    with pytest.raises(ValidationError):
        _request(required_hosts=["lab-host"])


def test_the_result_is_deterministic_for_the_same_input() -> None:
    first = HandlerLatencyQualification().handle(_request())
    second = HandlerLatencyQualification().handle(_request())
    assert first == second


def _prepared(
    build_ms: float | None, model_section_ms: float | None
) -> ModelContextCompactionPrepared:
    return ModelContextCompactionPrepared(
        candidate_id=UUID("22222222-2222-5222-8222-222222222222"),
        session_id="session-1",
        input_messages_hash="a" * 64,
        assembly_hash="b" * 64,
        version_vector_hash="c" * 64,
        profile_name="compaction_orchestrator",
        profile_content_hash="d" * 64,
        build_ms=build_ms,
        model_section_ms=model_section_ms,
    )


@pytest.mark.parametrize("model_section_ms", [0.0, 75.0, 250.0])
def test_round_trip_from_prepared_splits_build_phases(model_section_ms: float) -> None:
    trip = ModelCompactionRoundTrip.from_prepared(
        _prepared(250.0, model_section_ms),
        round_trip_id="trip-1",
        size="median",
        host="lab-host",
        temperature=EnumRoundTripTemperature.WARM,
    )
    assert trip == ModelCompactionRoundTrip(
        round_trip_id="trip-1",
        size="median",
        host="lab-host",
        temperature=EnumRoundTripTemperature.WARM,
        deterministic_build_ms=250.0 - model_section_ms,
        model_section_ms=model_section_ms,
    )


@pytest.mark.parametrize(
    ("build_ms", "model_section_ms", "message"),
    [
        (None, 75.0, "build_ms"),
        (250.0, None, "model_section_ms"),
        (250.0, 251.0, "model_section_ms"),
    ],
)
def test_round_trip_from_prepared_refuses_missing_or_inconsistent_timings(
    build_ms: float | None, model_section_ms: float | None, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        ModelCompactionRoundTrip.from_prepared(
            _prepared(build_ms, model_section_ms),
            round_trip_id="trip-1",
            size="median",
            host="lab-host",
            temperature=EnumRoundTripTemperature.COLD,
        )
