# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Scheduled consumer join golden and error chains."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import yaml
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.handlers.overlay_consumer_join_scheduled_tick import (
    LedgerRouteError,
    ModelOverlayConsumerJoinTickOutcome,
    run_overlay_consumer_join_tick,
)
from omnimarket.models.model_reading import EnumReadingOutcome, EnumReadingVerdict
from omnimarket.nodes.node_overlay_drift_compute.models.model_overlay_drift_compute import (
    ModelOverlayJoinCommand,
    ModelOverlayJoinResult,
)
from omnimarket.nodes.node_overlay_drift_compute_scheduled_effect.handlers.handler_overlay_drift_compute_scheduled import (
    HandlerOverlayDriftComputeScheduled,
)

pytestmark = pytest.mark.unit
NODE = "node_overlay_drift_compute_scheduled_effect"
T0 = datetime(2026, 11, 12, 23, 0, tzinfo=UTC)
REPO = Path(__file__).resolve().parents[3]


def tick(at: datetime) -> ModelRuntimeTick:
    return ModelRuntimeTick(
        now=at,
        tick_id=uuid4(),
        sequence_number=1,
        scheduled_at=at,
        correlation_id=uuid4(),
        scheduler_id="fixture",
        tick_interval_ms=1000,
    )


def schedule(root: Path) -> dict[str, str]:
    (root / NODE).mkdir()
    (root / NODE / "overlay.yaml").write_text(
        yaml.safe_dump(
            {"job_id": "join", "run_interval_seconds": 300, "grace_seconds": 60}
        ),
        encoding="utf-8",
    )
    return {"ONEX_SKILL_OVERLAY_ROOTS": str(root)}


def test_due_tick_and_interval_gate(tmp_path: Path) -> None:
    calls: list[datetime] = []

    def run(**kwargs: Any) -> ModelOverlayConsumerJoinTickOutcome:
        calls.append(kwargs["clock"]())
        return ModelOverlayConsumerJoinTickOutcome(False, "checked")

    h = HandlerOverlayDriftComputeScheduled(
        environ=schedule(tmp_path), tick=run, clock=lambda: T0
    )
    event = h.handle(tick(T0))
    assert event is not None
    assert (event.job_id, event.node_name, event.outcome, event.detail) == (
        "join",
        NODE,
        "completed",
        "checked",
    )
    assert h.handle(tick(T0 + timedelta(seconds=299))) is None
    assert h.handle(tick(T0 + timedelta(seconds=300))) is not None
    assert len(calls) == 2


def test_control_ignoring_gate_breaks_interval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = HandlerOverlayDriftComputeScheduled(
        environ=schedule(tmp_path),
        tick=lambda **_: ModelOverlayConsumerJoinTickOutcome(False, "ok"),
    )
    assert h.handle(tick(T0)) is not None

    def break_ignoring_gate(now: datetime) -> bool:
        return True

    monkeypatch.setattr(h._gate, "due", break_ignoring_gate)

    def assert_interval_gate() -> None:
        assert h.handle(tick(T0 + timedelta(seconds=1))) is None

    with pytest.raises(AssertionError):
        assert_interval_gate()


def test_missing_schedule_refuses_once(caplog: pytest.LogCaptureFixture) -> None:
    h = HandlerOverlayDriftComputeScheduled(environ={})
    with caplog.at_level(logging.ERROR):
        assert h.handle(tick(T0)) is None
        assert h.handle(tick(T0)) is None
    assert (
        len([r for r in caplog.records if r.levelname == "ERROR" and NODE in r.message])
        == 1
    )


@pytest.mark.parametrize(
    ("error", "detail"),
    [
        (
            NodeOverlayError("unreadable"),
            "the overlay consumer join overlay could not be read",
        ),
        (
            ValueError("unbound"),
            "the overlay consumer join sources are not bound in the runtime",
        ),
        (
            OSError("unbound"),
            "the overlay consumer join sources are not bound in the runtime",
        ),
        (
            LedgerRouteError("unbound"),
            "the overlay consumer join sources are not bound in the runtime",
        ),
    ],
)
def test_error_chain(tmp_path: Path, error: Exception, detail: str) -> None:
    def run(**kwargs: Any) -> ModelOverlayConsumerJoinTickOutcome:
        raise error

    event = HandlerOverlayDriftComputeScheduled(
        environ=schedule(tmp_path), tick=run
    ).handle(tick(T0))
    assert event is not None
    assert (event.outcome, event.detail) == ("failed", detail)


def test_control_failed_outcome_is_not_completed(tmp_path: Path) -> None:
    h = HandlerOverlayDriftComputeScheduled(
        environ=schedule(tmp_path),
        tick=lambda **_: ModelOverlayConsumerJoinTickOutcome(True, "failed"),
    )
    event = h.handle(tick(T0))
    assert event is not None
    assert (event.outcome, event.detail) == ("failed", "failed")


def test_tick_loads_real_overlay_and_maps_result(tmp_path: Path) -> None:
    overlay_dir = tmp_path / "node_overlay_drift_compute"
    overlay_dir.mkdir()
    (overlay_dir / "overlay.yaml").write_text(
        (
            REPO
            / "src/omnimarket/nodes/node_overlay_drift_compute/overlay.example.yaml"
        ).read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    commands: list[ModelOverlayJoinCommand] = []

    class FakeHandler:
        def handle(self, command: ModelOverlayJoinCommand) -> ModelOverlayJoinResult:
            commands.append(command)
            return ModelOverlayJoinResult(
                correlation_id=command.correlation_id,
                status="completed",
                host="fixture",
                verdict=EnumReadingVerdict.WORKING,
                outcome=EnumReadingOutcome.READ,
                consumers_checked=2,
                unreadable=("one",),
            )

    def build(overlay: Any, environ: Any, write: bool) -> tuple[FakeHandler, str]:
        assert overlay.consumers
        assert environ["ONEX_SKILL_OVERLAY_ROOTS"] == str(tmp_path)
        assert write is True
        return FakeHandler(), "fixture"

    result = run_overlay_consumer_join_tick(
        environ={"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)},
        clock=lambda: T0,
        build=build,
    )
    assert result == ModelOverlayConsumerJoinTickOutcome(
        False,
        "verdict=WORKING outcome=read consumers_checked=2 mismatches=0 park_lists=0 unreadable=1",
    )
    assert commands == [
        ModelOverlayJoinCommand(
            correlation_id="overlay-consumer-join-fixture-2026-11-12T23:00:00Z",
            write=True,
        )
    ]
