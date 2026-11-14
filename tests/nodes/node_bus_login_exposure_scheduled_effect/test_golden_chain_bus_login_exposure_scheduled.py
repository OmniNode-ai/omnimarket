# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chains of the bus login exposure probe's tick on the runtime schedule (OMN-24271)."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
import yaml
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.handlers.bus_login_exposure_scheduled_tick import (
    ModelBusLoginExposureTickOutcome,
    run_bus_login_exposure_tick,
)
from omnimarket.handlers.schedule_run_store import InMemoryScheduleRunStore
from omnimarket.models.model_reading import EnumReadingVerdict
from omnimarket.nodes.node_bus_login_exposure_effect.handlers.handler_bus_login_exposure import (
    HandlerBusLoginExposure,
)
from omnimarket.nodes.node_bus_login_exposure_effect.models.model_bus_login_exposure import (
    ModelBusLoginExposureReading,
    ModelBusLoginExposureRequest,
    ModelBusLoginExposureResult,
)
from omnimarket.nodes.node_bus_login_exposure_scheduled_effect.handlers.handler_bus_login_exposure_scheduled import (
    HandlerBusLoginExposureScheduled,
)
from omnimarket.nodes.node_schedule_run_reducer.handlers.handler_schedule_run_reducer import (
    HandlerScheduleRunReducer,
)

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]
EXAMPLE = (
    REPO / "src/omnimarket/nodes/node_bus_login_exposure_effect/overlay.example.yaml"
)
T0 = datetime(2026, 11, 12, 23, 0, tzinfo=UTC)
NODE = "node_bus_login_exposure_scheduled_effect"
PROBE = "node_bus_login_exposure_effect"
JOB = "bus_login_exposure"
EXTRA_TOPIC = "example.evt.private.secret.v1"


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
    tmp_path: Path,
    *,
    with_probe: bool = True,
    probe_document: dict[str, Any] | None = None,
) -> dict[str, str]:
    (tmp_path / NODE).mkdir()
    (tmp_path / NODE / "overlay.yaml").write_text(
        yaml.safe_dump(
            {
                "job_id": JOB,
                "run_interval_seconds": 300,
                "grace_seconds": 60,
                "exposure_overlay_id": PROBE,
            }
        ),
        encoding="utf-8",
    )
    if with_probe:
        (tmp_path / PROBE).mkdir()
        document = (
            probe_document
            if probe_document is not None
            else yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
        )
        (tmp_path / PROBE / "overlay.yaml").write_text(
            yaml.safe_dump(document), encoding="utf-8"
        )
    return {"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)}


def exposed_document() -> dict[str, Any]:
    """The example's clean view plus one topic the overlay does not allow."""
    document = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    document["source"]["fixture"]["topics"] = [
        *document["source"]["fixture"]["topics"],
        EXTRA_TOPIC,
    ]
    return document


def unbound_source_document() -> dict[str, Any]:
    """The Redpanda source whose login environment variables the runtime does not hold."""
    return {
        "settings": {
            "allowed_topics": ["example.evt.ci.completed.v1"],
            "allowed_topic_prefixes": ["example.cmd.ci."],
            "allowed_groups": [],
            "allowed_group_prefixes": ["example.ci.consume."],
            "max_age_seconds": 3600,
        },
        "source": {
            "backend": "redpanda",
            "bootstrap_servers": "broker.example.test:9092",
            "security_protocol": "SASL_PLAINTEXT",
            "sasl_mechanism": "SCRAM-SHA-512",
            "username_env": "EXAMPLE_BUS_LOGIN_USER",
            "password_env": "EXAMPLE_BUS_LOGIN_PASSWORD",
            "timeout_seconds": 15,
        },
    }


class _ExposedProbe:
    """A probe stand-in that always reports one exposed topic (NOT_WORKING)."""

    def handle(
        self, request: ModelBusLoginExposureRequest
    ) -> ModelBusLoginExposureResult:
        return ModelBusLoginExposureResult(
            correlation_id=request.correlation_id,
            backend="inmemory",
            reading=ModelBusLoginExposureReading(
                verdict=EnumReadingVerdict.NOT_WORKING,
                visible_topics=("example.evt.ci.completed.v1", EXTRA_TOPIC),
                visible_groups=(),
                exposed_topics=(EXTRA_TOPIC,),
                exposed_groups=(),
                read_at=request.now,
                stale=False,
            ),
        )


def test_golden_a_due_tick_runs_the_probe_once_and_returns_its_run_event(
    tmp_path: Path,
) -> None:
    calls: list[dict[str, Any]] = []

    def fake_tick(**kwargs: Any) -> ModelBusLoginExposureTickOutcome:
        calls.append(kwargs)
        return ModelBusLoginExposureTickOutcome(
            False, "WORKING exposed_topics=0 exposed_groups=0"
        )

    h = HandlerBusLoginExposureScheduled(environ=environ(tmp_path), tick=fake_tick)
    event = h.handle(tick(T0))
    assert event is not None
    assert (event.job_id, event.node_name) == (JOB, NODE)
    assert event.outcome == "completed"
    assert len(calls) == 1
    assert calls[0]["exposure_overlay_id"] == PROBE


def test_golden_a_clean_view_reads_a_completed_run(tmp_path: Path) -> None:
    h = HandlerBusLoginExposureScheduled(environ=environ(tmp_path))
    event = h.handle(tick(T0))
    assert event is not None
    assert (event.outcome, event.detail) == (
        "completed",
        "WORKING exposed_topics=0 exposed_groups=0",
    )


def test_golden_an_exposed_topic_reads_a_failed_run(tmp_path: Path) -> None:
    h = HandlerBusLoginExposureScheduled(
        environ=environ(tmp_path, probe_document=exposed_document())
    )
    event = h.handle(tick(T0))
    assert event is not None
    assert event.outcome == "failed"
    assert event.detail.startswith("NOT_WORKING")
    assert "exposed_topics=1" in event.detail
    # The detail counts the exposure; it never names the topic.
    assert EXTRA_TOPIC not in event.detail


def test_golden_the_gate_runs_once_per_interval(tmp_path: Path) -> None:
    h = HandlerBusLoginExposureScheduled(environ=environ(tmp_path))
    assert h.handle(tick(T0)) is not None
    assert h.handle(tick(T0 + timedelta(seconds=299))) is None
    assert h.handle(tick(T0 + timedelta(seconds=300))) is not None


def test_error_no_overlay_refuses_once_and_returns_none(
    caplog: pytest.LogCaptureFixture,
) -> None:
    h = HandlerBusLoginExposureScheduled(environ={})
    with caplog.at_level(logging.ERROR):
        assert h.handle(tick(T0)) is None
        assert h.handle(tick(T0 + timedelta(seconds=1))) is None
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(errors) == 1
    assert NODE in errors[0].message


def test_error_a_missing_probe_overlay_is_a_failed_run_naming_it(
    tmp_path: Path,
) -> None:
    h = HandlerBusLoginExposureScheduled(environ=environ(tmp_path, with_probe=False))
    event = h.handle(tick(T0))
    assert event is not None
    assert event.outcome == "failed"
    assert "the bus login exposure overlay could not be read" in event.detail


def test_error_a_source_without_its_login_is_a_failed_run_naming_it(
    tmp_path: Path,
) -> None:
    h = HandlerBusLoginExposureScheduled(
        environ=environ(tmp_path, probe_document=unbound_source_document())
    )
    event = h.handle(tick(T0))
    assert event is not None
    assert event.outcome == "failed"
    assert event.detail.startswith("UNKNOWN")
    assert "read_failure=credentials-missing" in event.detail


def test_control_failed_reading_is_a_failed_run(tmp_path: Path) -> None:
    def build(**kwargs: Any) -> HandlerBusLoginExposure:
        del kwargs
        return cast(HandlerBusLoginExposure, _ExposedProbe())

    def tick_with_fake_probe(**kwargs: Any) -> ModelBusLoginExposureTickOutcome:
        return run_bus_login_exposure_tick(**kwargs, build_handler=build)

    h = HandlerBusLoginExposureScheduled(
        environ=environ(tmp_path),
        tick=tick_with_fake_probe,
    )
    event = h.handle(tick(T0))
    assert event is not None
    assert event.outcome == "failed"
    assert "exposed_topics=1" in event.detail


def test_seam_the_run_event_folds_into_the_reducer_and_the_topics_match(
    tmp_path: Path,
) -> None:
    event = HandlerBusLoginExposureScheduled(environ=environ(tmp_path)).handle(tick(T0))
    assert event is not None
    store = InMemoryScheduleRunStore()
    assert HandlerScheduleRunReducer(store).handle(event).applied is True
    held = store.get(JOB)
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
