# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The scheduled job binds the lab alarm's lag source through the overlay's backend (OMN-24288).

The due tick hands the shared tick module the builder the job was constructed with, the lab alarm's
own ``build_handler``, so a ``runtime_admin`` overlay builds its handler over the admin reader and a
group the bound source cannot read is a failed run event that names it.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import yaml
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.handlers.lab_alarm_scheduled_tick import ModelLabAlarmTickOutcome
from omnimarket.nodes.node_lab_alarm.adapters.inmemory_adapters import (
    InMemoryLag,
    InMemoryManifest,
    InMemoryRunRecords,
    InMemorySamples,
)
from omnimarket.nodes.node_lab_alarm.adapters.runtime_adapters import AdminLag
from omnimarket.nodes.node_lab_alarm.handlers.handler_broker_lag import (
    HandlerBrokerLag,
)
from omnimarket.nodes.node_lab_alarm.overlay import ModelLabAlarmOverlay
from omnimarket.nodes.node_lab_alarm.runtime_lab_alarm import build_handler
from omnimarket.nodes.node_lab_alarm_scheduled_effect.handlers.handler_lab_alarm_scheduled import (
    HandlerLabAlarmScheduled,
)

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]
NODE = "node_lab_alarm_scheduled_effect"
ALARM_NODE = "node_lab_alarm"
ALARM_EXAMPLE = REPO / f"src/omnimarket/nodes/{ALARM_NODE}/overlay.example.yaml"
T0 = datetime(2026, 11, 12, 23, 0, tzinfo=UTC)
DONE = ModelLabAlarmTickOutcome(False, "outcome=ok declared=1")


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


def environ(tmp_path: Path) -> dict[str, str]:
    (tmp_path / NODE).mkdir()
    (tmp_path / NODE / "overlay.yaml").write_text(
        yaml.safe_dump(
            {
                "job_id": "lab_alarm",
                "run_interval_seconds": 300,
                "grace_seconds": 60,
                "alarm_overlay_id": ALARM_NODE,
            }
        ),
        encoding="utf-8",
    )
    return {"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)}


def alarm_environ(tmp_path: Path) -> dict[str, str]:
    """The lab alarm's own overlay, from the checked-in example."""
    (tmp_path / ALARM_NODE).mkdir()
    (tmp_path / ALARM_NODE / "overlay.yaml").write_text(
        ALARM_EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8"
    )
    return {"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)}


def bound_environ(tmp_path: Path) -> dict[str, str]:
    """Both overlays under one root: the schedule gate's and the lab alarm's own."""
    environ(tmp_path)
    return alarm_environ(tmp_path)


def test_the_job_hands_the_tick_the_alarms_own_builder(tmp_path: Path) -> None:
    """The recording tick requires the keyword, so a job that passed none fails with a TypeError."""
    calls: list[Callable[..., Any]] = []

    def recording_tick(
        *,
        alarm_overlay_id: str,
        environ: Mapping[str, str] | None,
        clock: Callable[[], datetime],
        build_handler: Callable[..., Any],
    ) -> ModelLabAlarmTickOutcome:
        calls.append(build_handler)
        return DONE

    h = HandlerLabAlarmScheduled(environ=environ(tmp_path), tick=recording_tick)
    event = h.handle(tick(T0))
    assert event is not None
    assert event.outcome == "completed"
    assert len(calls) == 1
    assert calls[0] is build_handler


def test_a_due_tick_with_a_bound_source_is_a_completed_run(tmp_path: Path) -> None:
    h = HandlerLabAlarmScheduled(
        environ=bound_environ(tmp_path),
        build_handler=lambda _overlay: _alarm_handler(_flat(7), _flat(7)),
    )
    event = h.handle(tick(T0))
    assert event is not None
    assert (event.outcome, event.detail) == ("completed", "outcome=ok declared=2")


def test_an_unreadable_group_in_the_bound_source_is_a_failed_run(
    tmp_path: Path,
) -> None:
    handler = _alarm_handler(_flat(7), _flat(7))
    vars(handler)["_lag"].refused[_group(_TOPICS[0])] = "the broker refused the read"
    h = HandlerLabAlarmScheduled(
        environ=bound_environ(tmp_path), build_handler=lambda _overlay: handler
    )
    event = h.handle(tick(T0))
    assert event is not None
    assert event.outcome == "failed"
    assert event.detail == (
        "the lab alarm could not read the lag: "
        f"{_group(_TOPICS[0])} (the broker refused the read)"
    )


def test_the_runtime_admin_backend_builds_a_handler_over_the_admin_reader() -> None:
    example = yaml.safe_load(ALARM_EXAMPLE.read_text(encoding="utf-8"))
    overlay = ModelLabAlarmOverlay.model_validate(
        {
            **example,
            "bus": {
                "backend": "runtime_admin",
                "lane": "dev",
                "bootstrap_servers": "broker.example.invalid:9092",
            },
        }
    )
    handler = build_handler(overlay)
    assert isinstance(handler, HandlerBrokerLag)
    assert isinstance(vars(handler)["_lag"], AdminLag)


_TOPICS = ("onex.evt.example.one.v1", "onex.evt.example.two.v1")


def _group(topic: str) -> str:
    return f"local.examplepkg.projection_example.consume.1.2.0.__i.runtime-main.__t.{topic}"


def _flat(value: int) -> dict[str, int]:
    return {_group(t): value for t in _TOPICS}


def _manifest() -> dict[str, object]:
    return {
        "contracts": [
            {
                "name": "projection_example",
                "package_name": "examplepkg",
                "contract_version": "1.2.0",
                "event_bus": {"subscribe_topics": list(_TOPICS)},
            }
        ]
    }


def _alarm_handler(lags: dict[str, int], previous: dict[str, int]) -> HandlerBrokerLag:
    return HandlerBrokerLag(
        overlay=ModelLabAlarmOverlay.model_validate(
            yaml.safe_load(ALARM_EXAMPLE.read_text(encoding="utf-8"))
        ),
        manifest=InMemoryManifest(_manifest()),
        lag=InMemoryLag(lags),
        samples=InMemorySamples(previous),
        records=InMemoryRunRecords(),
        clock=lambda: T0,
    )
