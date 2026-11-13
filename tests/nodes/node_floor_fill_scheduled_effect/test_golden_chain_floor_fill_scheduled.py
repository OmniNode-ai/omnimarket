# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chains of the fill's tick on the runtime schedule (OMN-24029)."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
import yaml
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.handlers.schedule_run_store import InMemoryScheduleRunStore
from omnimarket.nodes.node_floor_fill_orchestrator.handlers.handler_floor_fill_command import (
    HandlerFloorFillCommand,
)
from omnimarket.nodes.node_floor_fill_orchestrator.models import (
    ModelFloorFillCommandRequest,
    ModelFloorFillConfig,
    ModelFloorFillEntryResult,
)
from omnimarket.nodes.node_floor_fill_scheduled_effect.handlers.handler_floor_fill_scheduled import (
    HandlerFloorFillScheduled,
)
from omnimarket.nodes.node_schedule_run_reducer.handlers.handler_schedule_run_reducer import (
    HandlerScheduleRunReducer,
)

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]
EXAMPLE = (
    REPO / "src/omnimarket/nodes/node_floor_fill_orchestrator/overlay.example.yaml"
)
T0 = datetime(2026, 11, 12, 23, 0, tzinfo=UTC)
NODE = "node_floor_fill_scheduled_effect"


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
    tmp_path: Path, *, profile: str = "inmemory", with_fill: bool = True
) -> dict[str, str]:
    (tmp_path / NODE).mkdir()
    (tmp_path / NODE / "overlay.yaml").write_text(
        yaml.safe_dump(
            {
                "job_id": "floor_fill",
                "run_interval_seconds": 300,
                "grace_seconds": 60,
                "fill_overlay_id": "node_floor_fill_orchestrator",
                "profile": profile,
            }
        ),
        encoding="utf-8",
    )
    if with_fill:
        (tmp_path / "node_floor_fill_orchestrator").mkdir()
        (tmp_path / "node_floor_fill_orchestrator" / "overlay.yaml").write_text(
            EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8"
        )
    return {"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)}


class _Done:
    def __init__(self, exit_code: int, stdout: str) -> None:
        self._result = ModelFloorFillEntryResult(exit_code=exit_code, stdout=stdout)

    def handle(
        self, request: ModelFloorFillCommandRequest
    ) -> ModelFloorFillEntryResult:
        assert request.command.value == "tick"
        return self._result


def test_golden_a_due_tick_runs_the_fill_tick_and_reads_the_unread_floor_by_name(
    tmp_path: Path,
) -> None:
    h = HandlerFloorFillScheduled(environ=environ(tmp_path))
    event = h.handle(tick(T0))
    assert event is not None
    assert (event.job_id, event.node_name) == ("floor_fill", NODE)
    # The in-memory profile has no floor to read: the fill answers UNKNOWN, so the honest outcome is failed.
    assert event.outcome == "failed"
    assert event.detail.startswith("UNKNOWN")


def test_golden_a_fill_that_exits_zero_is_a_completed_run(tmp_path: Path) -> None:
    def build(config: ModelFloorFillConfig) -> HandlerFloorFillCommand:
        return cast(HandlerFloorFillCommand, _Done(0, "OK started 0 lanes\nmore\n"))

    h = HandlerFloorFillScheduled(
        environ=environ(tmp_path),
        build_handler=build,
    )
    event = h.handle(tick(T0))
    assert event is not None
    assert (event.outcome, event.detail) == ("completed", "OK started 0 lanes")


def test_golden_the_gate_runs_once_per_interval(tmp_path: Path) -> None:
    h = HandlerFloorFillScheduled(environ=environ(tmp_path))
    assert h.handle(tick(T0)) is not None
    assert h.handle(tick(T0 + timedelta(seconds=299))) is None
    assert h.handle(tick(T0 + timedelta(seconds=300))) is not None


def test_error_no_overlay_refuses_once_and_returns_none(
    caplog: pytest.LogCaptureFixture,
) -> None:
    h = HandlerFloorFillScheduled(environ={})
    with caplog.at_level(logging.ERROR):
        assert h.handle(tick(T0)) is None
        assert h.handle(tick(T0 + timedelta(seconds=1))) is None
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(errors) == 1
    assert NODE in errors[0].message


def test_error_a_missing_fill_overlay_is_a_failed_run_naming_it(
    tmp_path: Path,
) -> None:
    h = HandlerFloorFillScheduled(environ=environ(tmp_path, with_fill=False))
    event = h.handle(tick(T0))
    assert event is not None
    assert event.outcome == "failed"
    assert "node_floor_fill_orchestrator" in event.detail


def _example_dsn_env_name() -> str:
    return str(
        yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))["floor_fill_live"][
            "store_dsn_env"
        ]
    )


def test_error_the_live_profile_with_no_store_dsn_is_a_failed_run_naming_the_variable(
    tmp_path: Path,
) -> None:
    h = HandlerFloorFillScheduled(environ=environ(tmp_path, profile="live"))
    event = h.handle(tick(T0))
    assert event is not None
    assert event.outcome == "failed"
    assert (
        event.detail == f"UNKNOWN fill-state:store-dsn-unset:{_example_dsn_env_name()}"
    )


def test_golden_the_live_profile_builds_through_the_live_factory_with_the_named_dsn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, str] = {}

    def factory(settings: Any, *, dsn: str) -> object:
        seen["dsn"] = dsn
        seen["env"] = str(settings.store_dsn_env)

        def build(config: ModelFloorFillConfig) -> _Done:
            return _Done(0, "OK live pass\n")

        return build

    monkeypatch.setattr(
        "omnimarket.nodes.node_floor_fill_scheduled_effect.handlers."
        "handler_floor_fill_scheduled.build_live_handler_factory",
        factory,
    )
    env = environ(tmp_path, profile="live")
    env[_example_dsn_env_name()] = "postgres://fixture"
    event = HandlerFloorFillScheduled(environ=env).handle(tick(T0))
    assert event is not None
    assert (event.outcome, event.detail) == ("completed", "OK live pass")
    assert seen == {"dsn": "postgres://fixture", "env": _example_dsn_env_name()}
    assert "postgres://fixture" not in event.detail


def test_seam_the_run_event_folds_into_the_reducer_and_the_topics_match(
    tmp_path: Path,
) -> None:
    event = HandlerFloorFillScheduled(environ=environ(tmp_path)).handle(tick(T0))
    assert event is not None
    store = InMemoryScheduleRunStore()
    assert HandlerScheduleRunReducer(store).handle(event).applied is True
    held = store.get("floor_fill")
    assert held is not None
    assert held.last_outcome == "failed"
    producer = yaml.safe_load(
        (REPO / f"src/omnimarket/nodes/{NODE}/contract.yaml").read_text("utf-8")
    )
    reducer = yaml.safe_load(
        (
            REPO / "src/omnimarket/nodes/node_schedule_run_reducer/contract.yaml"
        ).read_text("utf-8")
    )
    assert producer["terminal_event"] == reducer["input_subscriptions"][0]["topic"]
