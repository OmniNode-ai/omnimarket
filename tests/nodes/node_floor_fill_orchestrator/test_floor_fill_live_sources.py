# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The capacity brake and the stop-mode policy of the live floor fill, bound by the one live binder.

The live binder (``build_live_handler_factory``) already reads the pool from the lane host pool snapshot and the brake and the
stop signal from the andon stop gate. Two things were not bound: a brake over the capacity the hosts advertise, and the
``andon_stop`` mode policy supplied by the live settings (``stop_modes``, ``stop_enforce_citations``) instead of a second overlay.

Golden: with fewer hosts admitting a lane than ``brake_min_admitting_hosts`` the fill holds (``open-count``) and starts nothing;
an enforced open stop of a settings-named class refuses and starts nothing, an observed one starts. Error: a stale snapshot is an
unread brake, not a released one; an open stop whose class the settings policy names no mode for refuses (``mode_missing``);
an enforced class with no citation is refused naming the key. Failing control: the same fill with the default bound starts lanes.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from omnimarket.models.stop_gate import ModelStopRecord, ModelStopScope
from omnimarket.nodes.node_floor_fill_orchestrator.handlers.adapter_floor_seams_live import (
    LiveSeamSources,
)
from omnimarket.nodes.node_floor_fill_orchestrator.models import (
    EXIT_CODE_BY_STATUS,
    EnumFloorFillStatus,
    FloorFillConfigError,
)
from omnimarket.nodes.node_floor_fill_orchestrator.models.model_floor_fill_live_settings import (
    load_floor_fill_live_settings,
)
from omnimarket.nodes.node_floor_fill_orchestrator.tests.test_floor_fill_live_binding import (
    live_section,
)
from omnimarket.nodes.node_floor_fill_orchestrator.tests.test_floor_fill_live_seam_binding import (
    PINNED,
    _live_tick,
)
from omnimarket.nodes.node_floor_task_ticket_effect.adapters.adapter_inmemory_floor_ticket_store import (
    InMemoryFloorTicketStore,
)

pytestmark = pytest.mark.unit

OK = EXIT_CODE_BY_STATUS[EnumFloorFillStatus.OK]
DEGRADED = EXIT_CODE_BY_STATUS[EnumFloorFillStatus.DEGRADED]

#: A stop class of the andon registry that is not the brake class, so it is read as the stop signal.
STOP_CLASS = "engine-policy-breach"


def _queue_with_stop(queue_dir: Path, *, cls: str = STOP_CLASS) -> None:
    """One open stop of ``cls`` in the exception queue directory the settings name (``andon_state_dir``)."""
    queue_dir.mkdir(exist_ok=True)
    stop = ModelStopRecord(
        id="stop-1",
        cls=cls,
        scope=ModelStopScope(kind="engine", key="claude_sonnet"),
        state="open",
        stopped_at=PINNED - timedelta(minutes=30),
    )
    row = {"id": "exc-1", "cls": cls, "stop": json.loads(stop.model_dump_json())}
    (queue_dir / "exception-queue.jsonl").write_text(
        json.dumps(row) + "\n", encoding="utf-8"
    )


@pytest.fixture
def queue_without_ledger(monkeypatch: pytest.MonkeyPatch) -> None:
    """Read the open stops of the queue file only: the ledger HOLD reader shells out to a ledger this test has none of."""
    from omnimarket.nodes.node_exception_queue_effect.container import (
        container_exception_queue_effect,
    )
    from omnimarket.nodes.node_exception_queue_effect.handler import HandlerOpenStops

    monkeypatch.setattr(
        container_exception_queue_effect,
        "create_handler_open_stops",
        lambda *_args, **_kwargs: HandlerOpenStops(),
    )


def _tick_over_queue(tmp_path: Path, **changes: Any) -> tuple[str, int, list[Any]]:
    """A live tick whose stop facts are the real queue read over ``andon_state_dir`` and the settings' own policy."""
    out, code, starter = _live_tick(
        tmp_path,
        live_changes=changes,
        sources=LiveSeamSources(
            ticket_store=lambda: InMemoryFloorTicketStore(), stop_facts=None
        ),
    )
    return out, code, starter.starts


def test_the_brake_engages_when_fewer_hosts_admit_than_the_bound(
    tmp_path: Path,
) -> None:
    """One host admits and the bound is two: the fill holds on ``open-count`` and starts nothing."""
    out, _code, starter = _live_tick(
        tmp_path,
        slots={"h051": 2},
        live_changes={"brake_min_admitting_hosts": 2},
    )
    assert "open-count" in out
    assert starter.starts == []


def test_control_the_default_bound_releases_the_brake_over_the_same_snapshot(
    tmp_path: Path,
) -> None:
    """The failing control: the same snapshot with the default bound (one host) starts lanes, so the brake is not constant."""
    out, code, starter = _live_tick(tmp_path, slots={"h051": 2})
    assert "open-count" not in out
    assert code == OK
    assert len(starter.starts) == 2


def test_error_a_stale_snapshot_is_an_unread_brake_not_a_released_one(
    tmp_path: Path,
) -> None:
    out, code, starter = _live_tick(tmp_path, pool_age=timedelta(seconds=181))
    assert "brake-unread:pool-snapshot" in out
    assert code == DEGRADED
    assert starter.starts == []


def test_error_a_snapshot_without_hosts_is_an_unread_brake_not_an_engaged_one(
    tmp_path: Path,
) -> None:
    out, code, starter = _live_tick(tmp_path, slots={})
    assert "brake-unread:pool-snapshot:no-host-readings" in out
    assert code == DEGRADED
    assert starter.starts == []


@pytest.mark.usefixtures("queue_without_ledger")
def test_the_stop_signal_refuses_an_enforced_class_the_settings_name(
    tmp_path: Path,
) -> None:
    _queue_with_stop(tmp_path / "andon")
    out, _code, starts = _tick_over_queue(
        tmp_path,
        stop_modes={STOP_CLASS: "enforce"},
        stop_enforce_citations={STOP_CLASS: "ruling"},
    )
    assert "stop-signal" in out
    assert "stop-1" in out or STOP_CLASS in out
    assert starts == []


@pytest.mark.usefixtures("queue_without_ledger")
def test_the_stop_signal_observes_a_class_the_settings_put_in_observe_mode(
    tmp_path: Path,
) -> None:
    _queue_with_stop(tmp_path / "andon")
    out, code, starts = _tick_over_queue(tmp_path, stop_modes={STOP_CLASS: "observe"})
    assert "stop-signal" not in out
    assert code == OK
    assert len(starts) >= 1


@pytest.mark.usefixtures("queue_without_ledger")
def test_error_an_open_stop_with_no_mode_in_the_settings_refuses(
    tmp_path: Path,
) -> None:
    _queue_with_stop(tmp_path / "andon")
    out, _code, starts = _tick_over_queue(tmp_path, stop_modes={})
    assert "mode_missing" in out
    assert starts == []


def test_error_an_enforced_class_without_a_citation_is_refused_naming_the_key(
    tmp_path: Path,
) -> None:
    with pytest.raises(FloorFillConfigError) as refused:
        load_floor_fill_live_settings(
            live_section(tmp_path, stop_modes={STOP_CLASS: "enforce"})
        )
    assert "floor_fill_live.stop_enforce_citations" in str(refused.value)


def test_the_new_keys_are_optional_and_the_pool_keys_are_not_redefined(
    tmp_path: Path,
) -> None:
    """An overlay with none of the new keys loads: the bound defaults to one host and no policy is supplied."""
    settings = load_floor_fill_live_settings(live_section(tmp_path))
    assert settings.brake_min_admitting_hosts == 1
    assert settings.stop_modes is None
    assert settings.pool_snapshot_path == str(tmp_path / "pool.json")
    assert settings.andon_state_dir == str(tmp_path / "andon")
