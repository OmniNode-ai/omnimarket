# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chains of the C32 probe's tick on the runtime schedule (OMN-24271)."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.handlers.schedule_run_store import InMemoryScheduleRunStore
from omnimarket.nodes.node_c32_auth_boundary_probe_effect.handlers.adapter_c32_probers import (
    InMemoryC32Prober,
    InMemoryC32Publisher,
)
from omnimarket.nodes.node_c32_auth_boundary_probe_effect.models.model_c32_probe import (
    ModelC32Clause,
    ModelC32ProbeReading,
)
from omnimarket.nodes.node_c32_probe_scheduled_effect.handlers.handler_c32_probe_scheduled import (
    HandlerC32ProbeScheduled,
)
from omnimarket.nodes.node_schedule_run_reducer.handlers.handler_schedule_run_reducer import (
    HandlerScheduleRunReducer,
)

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]
T0 = datetime(2026, 11, 12, 23, 0, tzinfo=UTC)
NODE = "node_c32_probe_scheduled_effect"
PROBE_NODE = "node_c32_auth_boundary_probe_effect"
RESULT_TOPIC = "onex.evt.omnimarket.local-auth-boundary-probe-completed.v1"


def tick(at: datetime) -> ModelRuntimeTick:
    return ModelRuntimeTick(
        now=at,
        tick_id=uuid4(),
        sequence_number=1,
        scheduled_at=at,
        correlation_id=uuid4(),
        scheduler_id="runtime-fixture",
        tick_interval_ms=1000,
    )


def environ(
    tmp_path: Path, *, with_probe: bool = True, result_topic: str = RESULT_TOPIC
) -> dict[str, str]:
    (tmp_path / NODE).mkdir()
    (tmp_path / NODE / "overlay.yaml").write_text(
        yaml.safe_dump(
            {
                "job_id": "c32_probe",
                "run_interval_seconds": 300,
                "grace_seconds": 60,
                "probe_overlay_id": PROBE_NODE,
            }
        ),
        encoding="utf-8",
    )
    if with_probe:
        (tmp_path / PROBE_NODE).mkdir()
        (tmp_path / PROBE_NODE / "overlay.yaml").write_text(
            yaml.safe_dump(
                {
                    "default_profile": "inmemory",
                    "history_file": "c32-runs.jsonl",
                    "history_keep": 60,
                    "result_topic": result_topic,
                    "profiles": {"inmemory": {"kind": "inmemory"}},
                }
            ),
            encoding="utf-8",
        )
    return {"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)}


def reading(clause_ok: bool) -> ModelC32ProbeReading:
    return ModelC32ProbeReading(
        clauses=(
            ModelC32Clause(
                name="a stale token is refused",
                ok=clause_ok,
                detail="refused" if clause_ok else "served",
            ),
        )
    )


class _CountingProber:
    """A prober that counts its runs, so the test can see the probe ran once."""

    def __init__(self, probe_reading: ModelC32ProbeReading) -> None:
        self._reading = probe_reading
        self.runs = 0

    def run_probe(self) -> ModelC32ProbeReading:
        self.runs += 1
        return self._reading


def test_golden_a_due_tick_runs_the_probe_once_and_returns_the_run_event(
    tmp_path: Path,
) -> None:
    prober = _CountingProber(reading(clause_ok=True))
    publisher = InMemoryC32Publisher()
    h = HandlerC32ProbeScheduled(
        environ=environ(tmp_path), prober=prober, publisher=publisher
    )
    event = h.handle(tick(T0))
    assert event is not None
    assert (event.job_id, event.node_name) == ("c32_probe", NODE)
    assert event.outcome == "completed"
    assert event.detail == "verdict=PASS clauses_ok=1/1 profile=inmemory"
    assert (prober.runs, len(publisher.events)) == (1, 1)
    assert publisher.events[0]["topic"] == RESULT_TOPIC


def test_golden_the_gate_runs_once_per_interval(tmp_path: Path) -> None:
    h = HandlerC32ProbeScheduled(
        environ=environ(tmp_path),
        prober=InMemoryC32Prober(reading(clause_ok=True)),
        publisher=InMemoryC32Publisher(),
    )
    assert h.handle(tick(T0)) is not None
    assert h.handle(tick(T0 + timedelta(seconds=299))) is None
    assert h.handle(tick(T0 + timedelta(seconds=300))) is not None


def test_error_no_overlay_refuses_once_and_returns_none(
    caplog: pytest.LogCaptureFixture,
) -> None:
    h = HandlerC32ProbeScheduled(environ={})
    with caplog.at_level(logging.ERROR):
        assert h.handle(tick(T0)) is None
        assert h.handle(tick(T0 + timedelta(seconds=1))) is None
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(errors) == 1
    assert NODE in errors[0].message


def test_error_a_missing_probe_overlay_is_a_failed_run_naming_it(
    tmp_path: Path,
) -> None:
    h = HandlerC32ProbeScheduled(environ=environ(tmp_path, with_probe=False))
    event = h.handle(tick(T0))
    assert event is not None
    assert event.outcome == "failed"
    assert PROBE_NODE in event.detail
    assert "the C32 probe overlay could not be read" in event.detail


def test_error_an_unbound_result_topic_is_a_failed_run_naming_it(
    tmp_path: Path,
) -> None:
    unbound = "onex.evt.example.c32-unbound.v1"
    h = HandlerC32ProbeScheduled(
        environ=environ(tmp_path, result_topic=unbound),
        prober=InMemoryC32Prober(reading(clause_ok=True)),
        publisher=InMemoryC32Publisher(),
    )
    event = h.handle(tick(T0))
    assert event is not None
    assert event.outcome == "failed"
    assert unbound in event.detail


def test_error_a_publisher_that_refuses_the_record_is_a_failed_run(
    tmp_path: Path,
) -> None:
    h = HandlerC32ProbeScheduled(
        environ=environ(tmp_path),
        prober=InMemoryC32Prober(reading(clause_ok=True)),
        publisher=InMemoryC32Publisher(accept=False),
    )
    event = h.handle(tick(T0))
    assert event is not None
    assert event.outcome == "failed"
    assert "the probe record was not accepted by the bus" in event.detail


def test_control_failed_reading_is_a_failed_run(tmp_path: Path) -> None:
    h = HandlerC32ProbeScheduled(
        environ=environ(tmp_path),
        prober=InMemoryC32Prober(reading(clause_ok=False)),
        publisher=InMemoryC32Publisher(),
    )
    event = h.handle(tick(T0))
    assert event is not None
    assert event.outcome == "failed"
    assert "verdict=FAIL clauses_ok=0/1" in event.detail


def test_seam_the_run_event_folds_into_the_reducer_and_the_topics_match(
    tmp_path: Path,
) -> None:
    event = HandlerC32ProbeScheduled(
        environ=environ(tmp_path),
        prober=InMemoryC32Prober(reading(clause_ok=True)),
        publisher=InMemoryC32Publisher(),
    ).handle(tick(T0))
    assert event is not None
    store = InMemoryScheduleRunStore()
    assert HandlerScheduleRunReducer(store).handle(event).applied is True
    held = store.get("c32_probe")
    assert held is not None
    assert held.last_outcome == "completed"
    producer = yaml.safe_load(
        (REPO / f"src/omnimarket/nodes/{NODE}/contract.yaml").read_text("utf-8")
    )
    reducer = yaml.safe_load(
        (
            REPO / "src/omnimarket/nodes/node_schedule_run_reducer/contract.yaml"
        ).read_text("utf-8")
    )
    assert producer["terminal_event"] == reducer["input_subscriptions"][0]["topic"]
