# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chains of the overlay drift read on the runtime schedule (OMN-24271)."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import yaml
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.handlers.overlay_drift_scheduled_tick import LedgerRouteError
from omnimarket.handlers.schedule_run_store import InMemoryScheduleRunStore
from omnimarket.models.model_reading import EnumReadingVerdict
from omnimarket.nodes.node_overlay_drift_effect.models.model_overlay_drift import (
    ModelOverlayDriftCommand,
)
from omnimarket.nodes.node_overlay_drift_effect.overlay import ModelOverlayDriftOverlay
from omnimarket.nodes.node_overlay_drift_scheduled_effect.handlers.handler_overlay_drift_scheduled import (
    HandlerOverlayDriftScheduled,
)
from omnimarket.nodes.node_schedule_run_reducer.handlers.handler_schedule_run_reducer import (
    HandlerScheduleRunReducer,
)

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]
T0 = datetime(2026, 11, 12, 23, 0, tzinfo=UTC)
NODE = "node_overlay_drift_scheduled_effect"
DRIFT = "node_overlay_drift_effect"

#: The drift read's own overlay, the required keys only: with a fake build the
#: environment variables the overlay names are never read, and the optional
#: upstream, emission and pin_history blocks stay absent so no ledger is touched.
DRIFT_OVERLAY: dict[str, Any] = {
    "home": {
        "root_env": "EXAMPLE_OVERLAY_HOME",
        "overlays_subdir": "overlays",
        "overlay_file": "overlay.yaml",
        "pins_file": ".example-node-pins",
    },
    "runtime": {
        "name": "example-runtime",
        "python_env": "EXAMPLE_RUNTIME_PYTHON",
        "timeout_seconds": 120,
    },
    "ledger_append": {
        "command_env": "EXAMPLE_LEDGER_APPEND_COMMAND",
        "host_env": "EXAMPLE_HOST_NAME",
        "timeout_seconds": 60,
    },
    "publish": {
        "lane": "overlay-drift",
        "actor": "node:node_overlay_drift_effect",
        "publish_max_age_seconds": 129600,
        "max_named": 10,
    },
    "injected_fault": "An overlay key the runtime rejects (the rung_order case)",
}


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


def environ(tmp_path: Path, *, with_drift: bool = True) -> dict[str, str]:
    (tmp_path / NODE).mkdir()
    (tmp_path / NODE / "overlay.yaml").write_text(
        yaml.safe_dump(
            {
                "job_id": "overlay_drift",
                "run_interval_seconds": 300,
                "grace_seconds": 60,
                "drift_overlay_id": DRIFT,
            }
        ),
        encoding="utf-8",
    )
    if with_drift:
        (tmp_path / DRIFT).mkdir()
        (tmp_path / DRIFT / "overlay.yaml").write_text(
            yaml.safe_dump(DRIFT_OVERLAY), encoding="utf-8"
        )
    return {"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)}


@dataclass(frozen=True)
class _FakeCounts:
    checked: int = 2
    accepted: int = 2
    rejected: int = 0


@dataclass(frozen=True)
class _FakeReading:
    """The fields of ModelOverlayDriftResult the tick's mapping reads."""

    status: str = "completed"
    failure: str | None = None
    failed: bool = False
    verdict: Any = EnumReadingVerdict.WORKING
    counts: _FakeCounts = _FakeCounts()


class _FakeHandler:
    """The drift read with no home, runtime, git or ledger behind it."""

    def __init__(self, reading: _FakeReading) -> None:
        self._reading = reading
        self.commands: list[ModelOverlayDriftCommand] = []

    def handle(self, command: ModelOverlayDriftCommand) -> _FakeReading:
        self.commands.append(command)
        return self._reading


class _FakeBuild:
    """The seam the drift runtime's build_handler stands in for."""

    def __init__(self, reading: _FakeReading) -> None:
        self.handler = _FakeHandler(reading)
        self.calls: list[tuple[ModelOverlayDriftOverlay, Mapping[str, str], bool]] = []

    def __call__(
        self, overlay: ModelOverlayDriftOverlay, environ: Mapping[str, str], write: bool
    ) -> tuple[_FakeHandler, str]:
        self.calls.append((overlay, environ, write))
        return self.handler, "example-host"


class _UnboundBuild:
    """A build whose sources are not bound: it raises the way build_handler does."""

    def __init__(self, error: Exception) -> None:
        self._error = error

    def __call__(
        self, overlay: ModelOverlayDriftOverlay, environ: Mapping[str, str], write: bool
    ) -> Any:
        raise self._error


def test_golden_a_due_tick_runs_the_drift_read_once_and_returns_the_run_event(
    tmp_path: Path,
) -> None:
    build = _FakeBuild(_FakeReading())
    h = HandlerOverlayDriftScheduled(
        environ=environ(tmp_path), build=build, clock=lambda: T0
    )
    event = h.handle(tick(T0))
    assert event is not None
    assert (event.job_id, event.node_name) == ("overlay_drift", NODE)
    assert event.outcome == "completed"
    assert event.detail == "verdict=WORKING checked=2 accepted=2 rejected=0"
    assert len(build.calls) == 1
    overlay, env, write = build.calls[0]
    assert write is True
    assert overlay.injected_fault.startswith("An overlay key")
    assert env["ONEX_SKILL_OVERLAY_ROOTS"] == str(tmp_path)
    assert len(build.handler.commands) == 1
    command = build.handler.commands[0]
    assert command.write is True
    assert command.correlation_id == "overlay-drift-example-host-2026-11-12T23:00:00Z"


def test_golden_the_gate_runs_once_per_interval(tmp_path: Path) -> None:
    build = _FakeBuild(_FakeReading())
    h = HandlerOverlayDriftScheduled(environ=environ(tmp_path), build=build)
    assert h.handle(tick(T0)) is not None
    assert h.handle(tick(T0 + timedelta(seconds=299))) is None
    assert h.handle(tick(T0 + timedelta(seconds=300))) is not None
    assert len(build.calls) == 2


def test_error_no_overlay_refuses_once_and_returns_none(
    caplog: pytest.LogCaptureFixture,
) -> None:
    h = HandlerOverlayDriftScheduled(environ={})
    with caplog.at_level(logging.ERROR):
        assert h.handle(tick(T0)) is None
        assert h.handle(tick(T0 + timedelta(seconds=1))) is None
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(errors) == 1
    assert NODE in errors[0].message


def test_error_a_missing_drift_overlay_is_a_failed_run_naming_it(
    tmp_path: Path,
) -> None:
    h = HandlerOverlayDriftScheduled(environ=environ(tmp_path, with_drift=False))
    event = h.handle(tick(T0))
    assert event is not None
    assert event.node_name == NODE
    assert event.outcome == "failed"
    assert event.detail == "the overlay drift overlay could not be read"


def test_error_unbound_sources_are_a_failed_run_naming_it(tmp_path: Path) -> None:
    for index, error in enumerate(
        (
            ValueError(
                "EXAMPLE_RUNTIME_PYTHON is not set; the node_overlay_drift_effect overlay names it"
            ),
            OSError("the overlay home clone is not readable"),
            LedgerRouteError("EXAMPLE_LEDGER_PATH is not set"),
        )
    ):
        root = tmp_path / str(index)
        root.mkdir()
        h = HandlerOverlayDriftScheduled(
            environ=environ(root), build=_UnboundBuild(error)
        )
        event = h.handle(tick(T0))
        assert event is not None
        assert event.node_name == NODE
        assert event.outcome == "failed"
        assert event.detail == "the overlay drift sources are not bound in the runtime"


def test_control_failed_reading_is_a_failed_run(tmp_path: Path) -> None:
    # A reading that ran but reports failed (the row did not land) must not read
    # as completed: this test goes red if the mapping always says completed.
    build = _FakeBuild(
        _FakeReading(
            failed=True,
            verdict=EnumReadingVerdict.UNKNOWN,
            counts=_FakeCounts(checked=1, accepted=0, rejected=0),
        )
    )
    h = HandlerOverlayDriftScheduled(environ=environ(tmp_path), build=build)
    event = h.handle(tick(T0))
    assert event is not None
    assert event.outcome == "failed"


def test_seam_the_run_event_folds_into_the_reducer_and_the_topics_match(
    tmp_path: Path,
) -> None:
    build = _FakeBuild(_FakeReading())
    event = HandlerOverlayDriftScheduled(environ=environ(tmp_path), build=build).handle(
        tick(T0)
    )
    assert event is not None
    store = InMemoryScheduleRunStore()
    assert HandlerScheduleRunReducer(store).handle(event).applied is True
    held = store.get("overlay_drift")
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
