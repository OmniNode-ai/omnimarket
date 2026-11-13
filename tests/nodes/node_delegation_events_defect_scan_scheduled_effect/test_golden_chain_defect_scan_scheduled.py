# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chains of the defect scan's tick on the runtime schedule (OMN-24271)."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.handlers.defect_scan_scheduled_tick import LedgerRouteError
from omnimarket.handlers.schedule_run_store import InMemoryScheduleRunStore
from omnimarket.models.model_delegation_events_defect_scan import ModelDefectScanOverlay
from omnimarket.models.model_reading import EnumReadingVerdict
from omnimarket.nodes.node_delegation_events_defect_scan_effect.models.model_defect_scan_run import (
    ModelDefectScanCommand,
    ModelDefectScanRunResult,
)
from omnimarket.nodes.node_delegation_events_defect_scan_effect.overlay import (
    ModelDefectScanEffectOverlay,
)
from omnimarket.nodes.node_delegation_events_defect_scan_scheduled_effect.handlers.handler_defect_scan_scheduled import (
    HandlerDefectScanScheduled,
)
from omnimarket.nodes.node_schedule_run_reducer.handlers.handler_schedule_run_reducer import (
    HandlerScheduleRunReducer,
)

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]
T0 = datetime(2026, 11, 12, 23, 0, tzinfo=UTC)
NODE = "node_delegation_events_defect_scan_scheduled_effect"
SCAN_EFFECT = "node_delegation_events_defect_scan_effect"
SCAN_COMPUTE = "node_delegation_events_defect_scan_compute"

# Synthetic overlays shaped like the deployed ones: the effect's routes and the compute node's day.
EFFECT_OVERLAY = {
    "events_source": {
        "ssh_host_env": "TEST_RUNTIME_SSH_HOST",
        "compose_project": "test-project",
        "service": "postgres",
        "database": "test_analytics",
        "db_user": "test_user",
        "read_timeout_seconds": 30,
    },
    "ledger_append": {
        "command_env": "TEST_LEDGER_APPEND_COMMAND",
        "host_env": "TEST_LEDGER_HOST_NAME",
        "timeout_seconds": 60,
    },
    "publish": {"actor": f"node:{SCAN_EFFECT}"},
}
SCAN_OVERLAY = {
    "window_seconds": 86400,
    "terminal_grace_seconds": 3600,
    "real_data_source": "real",
    "publish_max_age_seconds": 172800,
    "row_lane": "delegation-defect-scan",
    "row_src": "delegation-defect-scan",
    "bounds": {
        "min_rows_scanned": 50,
        "max_missing_terminal": 0,
        "max_cost_unread_as_zero": 0,
        "max_unattributed": 0,
    },
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


def environ(tmp_path: Path, *, with_scan: bool = True) -> dict[str, str]:
    (tmp_path / NODE).mkdir()
    (tmp_path / NODE / "overlay.yaml").write_text(
        yaml.safe_dump(
            {
                "job_id": "defect_scan",
                "run_interval_seconds": 300,
                "grace_seconds": 60,
                "scan_effect_overlay_id": SCAN_EFFECT,
                "scan_compute_overlay_id": SCAN_COMPUTE,
            }
        ),
        encoding="utf-8",
    )
    if with_scan:
        for overlay_id, overlay in (
            (SCAN_EFFECT, EFFECT_OVERLAY),
            (SCAN_COMPUTE, SCAN_OVERLAY),
        ):
            (tmp_path / overlay_id).mkdir()
            (tmp_path / overlay_id / "overlay.yaml").write_text(
                yaml.safe_dump(overlay), encoding="utf-8"
            )
    return {"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)}


def reading(
    *,
    status: str = "read",
    verdict: EnumReadingVerdict = EnumReadingVerdict.NOT_WORKING,
    failure: str | None = None,
) -> ModelDefectScanRunResult:
    return ModelDefectScanRunResult(
        correlation_id="scan-run-1",
        status=status,  # type: ignore[arg-type]
        verdict=verdict,
        reasons=(),
        rows_scanned=120,
        missing_terminal=0,
        cost_unread_as_zero=0,
        unattributed=2,
        ledger="in-memory",
        row="delegation-defect-scan",
        failure=failure,
        events=(),
    )


class _ScanSpy:
    """A stand-in for the scan's run handler: it records the command it was given."""

    def __init__(self, result: ModelDefectScanRunResult) -> None:
        self._result = result
        self.commands: list[ModelDefectScanCommand] = []

    def handle(self, command: ModelDefectScanCommand) -> ModelDefectScanRunResult:
        self.commands.append(command)
        return self._result


class _SpyBuild:
    """A build that stands in for the scan unit's build_handler and records what it built."""

    def __init__(self, result: ModelDefectScanRunResult) -> None:
        self._result = result
        self.built: list[tuple[object, object]] = []
        self.handlers: list[_ScanSpy] = []

    def __call__(
        self,
        overlay: object,
        scan_overlay: object,
        environ: Mapping[str, str],
        write: bool,
    ) -> _ScanSpy:
        assert write is True
        self.built.append((overlay, scan_overlay))
        handler = _ScanSpy(self._result)
        self.handlers.append(handler)
        return handler


def test_golden_a_due_tick_runs_the_scan_once_and_reports_the_reading(
    tmp_path: Path,
) -> None:
    build = _SpyBuild(reading())
    h = HandlerDefectScanScheduled(environ=environ(tmp_path), build=build)
    event = h.handle(tick(T0))
    assert event is not None
    assert (event.job_id, event.node_name) == ("defect_scan", NODE)
    assert len(build.handlers) == 1
    assert len(build.handlers[0].commands) == 1
    assert build.handlers[0].commands[0].write is True
    # The schedule overlay's ids were followed: the tick loaded the effect's and the compute node's overlays.
    assert isinstance(build.built[0][0], ModelDefectScanEffectOverlay)
    assert isinstance(build.built[0][1], ModelDefectScanOverlay)
    # A read of NOT WORKING is still a completed run: the scan ran and wrote its row.
    assert event.outcome == "completed"
    assert (
        event.detail == "verdict=NOT_WORKING rows=120 missing_terminal=0 unattributed=2"
    )


def test_control_failed_reading_is_a_failed_run(tmp_path: Path) -> None:
    build = _SpyBuild(
        reading(
            status="failed",
            verdict=EnumReadingVerdict.UNKNOWN,
            failure="the row did not land",
        )
    )
    h = HandlerDefectScanScheduled(environ=environ(tmp_path), build=build)
    event = h.handle(tick(T0))
    assert event is not None
    assert event.outcome == "failed"
    assert event.detail == "the row did not land"


def test_golden_the_gate_runs_once_per_interval(tmp_path: Path) -> None:
    build = _SpyBuild(reading())
    h = HandlerDefectScanScheduled(environ=environ(tmp_path), build=build)
    assert h.handle(tick(T0)) is not None
    assert len(build.handlers) == 1
    assert h.handle(tick(T0 + timedelta(seconds=299))) is None
    assert len(build.handlers) == 1
    assert h.handle(tick(T0 + timedelta(seconds=300))) is not None
    assert len(build.handlers) == 2


def test_error_no_overlay_refuses_once_and_returns_none(
    caplog: pytest.LogCaptureFixture,
) -> None:
    h = HandlerDefectScanScheduled(environ={})
    with caplog.at_level(logging.ERROR):
        assert h.handle(tick(T0)) is None
        assert h.handle(tick(T0 + timedelta(seconds=1))) is None
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(errors) == 1
    assert NODE in errors[0].message


def test_error_a_missing_scan_overlay_is_a_failed_run_naming_it(
    tmp_path: Path,
) -> None:
    build = _SpyBuild(reading())
    h = HandlerDefectScanScheduled(
        environ=environ(tmp_path, with_scan=False), build=build
    )
    event = h.handle(tick(T0))
    assert event is not None
    assert event.outcome == "failed"
    assert SCAN_EFFECT in event.detail
    assert build.built == []


def test_error_sources_not_bound_is_a_failed_run_naming_it(tmp_path: Path) -> None:
    def unbound_route(
        overlay: object, scan_overlay: object, environ: Mapping[str, str], write: bool
    ) -> _ScanSpy:
        raise LedgerRouteError("TEST_LEDGER_APPEND_COMMAND is not set")

    event = HandlerDefectScanScheduled(
        environ=environ(tmp_path), build=unbound_route
    ).handle(tick(T0))
    assert event is not None
    assert event.outcome == "failed"
    assert event.detail == "the defect scan sources are not bound in the runtime"

    def unbound_collaborator(
        overlay: object, scan_overlay: object, environ: Mapping[str, str], write: bool
    ) -> _ScanSpy:
        raise KeyError("TEST_RUNTIME_SSH_HOST")

    second = tmp_path / "second"
    second.mkdir()
    event = HandlerDefectScanScheduled(
        environ=environ(second), build=unbound_collaborator
    ).handle(tick(T0 + timedelta(seconds=300)))
    assert event is not None
    assert event.outcome == "failed"
    assert event.detail == "the defect scan sources are not bound in the runtime"


def test_seam_the_run_event_folds_into_the_reducer_and_the_topics_match(
    tmp_path: Path,
) -> None:
    build = _SpyBuild(reading())
    event = HandlerDefectScanScheduled(environ=environ(tmp_path), build=build).handle(
        tick(T0)
    )
    assert event is not None
    store = InMemoryScheduleRunStore()
    assert HandlerScheduleRunReducer(store).handle(event).applied is True
    held = store.get("defect_scan")
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
