# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The live profile of the fill's tick on the runtime schedule (OMN-24307)."""

from __future__ import annotations

import typing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import yaml
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.handlers.floor_fill_scheduled_tick import run_floor_fill_tick
from omnimarket.nodes.node_floor_fill_orchestrator.orchestration.core import (
    ANSWER_ROW_SUFFIX,
)
from omnimarket.nodes.node_floor_fill_orchestrator.orchestration.ports import (
    FloorFillUnreadableError,
    ModelStateRead,
)
from omnimarket.nodes.node_floor_fill_orchestrator.orchestration.store import (
    InMemoryFloorStateStore,
)
from omnimarket.nodes.node_floor_fill_scheduled_effect.handlers.handler_floor_fill_scheduled import (
    HandlerFloorFillScheduled,
)
from omnimarket.nodes.node_floor_fill_scheduled_effect.models import (
    ModelFloorFillScheduleOverlay,
)

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[5]
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
    tmp_path: Path,
    *,
    dsn: str | None = "postgresql://fixture-user:SENTINEL-PW@fixture-host/db",
) -> dict[str, str]:
    (tmp_path / NODE).mkdir()
    (tmp_path / NODE / "overlay.yaml").write_text(
        yaml.safe_dump(
            {
                "job_id": "floor_fill",
                "run_interval_seconds": 300,
                "grace_seconds": 60,
                "fill_overlay_id": "node_floor_fill_orchestrator",
                "profile": "live",
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "node_floor_fill_orchestrator").mkdir()
    (tmp_path / "node_floor_fill_orchestrator" / "overlay.yaml").write_text(
        EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8"
    )
    env = {"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)}
    if dsn is not None:
        env["EXAMPLE_FLOOR_FILL_DSN"] = dsn
    return env


def _scope() -> str:
    document = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    return str(document["floor_fill"]["scope"])


def test_live_golden_a_due_live_tick_writes_the_answer_row(tmp_path: Path) -> None:
    env = environ(tmp_path)
    store = InMemoryFloorStateStore()
    handler = HandlerFloorFillScheduled(environ=env, store=store)
    event = handler.handle(tick(T0))
    assert event is not None
    row = store.load(_scope() + ANSWER_ROW_SUFFIX)
    assert row.payload is not None
    assert row.version >= 1
    # The seams that have no live source are unbound, so the stored answer is the honest degraded one, naming each.
    assert '"status":"DEGRADED"' in row.payload
    assert "UNKNOWN pool-unread:unbound:pool" in row.payload
    assert "SENTINEL-PW" not in event.detail
    assert "SENTINEL-PW" not in event.model_dump_json()
    assert event.outcome == "failed"
    assert event.detail.startswith("UNKNOWN floor-unread")


def test_error_dsn_unset_names_the_variable_not_a_value(tmp_path: Path) -> None:
    env = environ(tmp_path, dsn=None)
    handler = HandlerFloorFillScheduled(environ=env, store=InMemoryFloorStateStore())
    event = handler.handle(tick(T0))
    assert event is not None
    assert event.outcome == "failed"
    assert event.detail == "UNKNOWN fill-state:store-dsn-unset:EXAMPLE_FLOOR_FILL_DSN"


def test_error_unreachable_store_is_a_failed_run_naming_the_cause(
    tmp_path: Path,
) -> None:
    class UnreachableStore:
        def load(self, key: str) -> ModelStateRead:
            raise FloorFillUnreadableError("store-unreachable:OperationalError")

        def compare_and_set(
            self, key: str, expected_version: int, new_version: int, payload: str
        ) -> bool:
            raise FloorFillUnreadableError("store-unreachable:OperationalError")

    handler = HandlerFloorFillScheduled(
        environ=environ(tmp_path), store=UnreachableStore()
    )
    event = handler.handle(tick(T0))
    assert event is not None
    assert event.outcome == "failed"
    assert event.detail == "UNKNOWN fill-state:store-unreachable:OperationalError"


def test_live_without_injected_store_builds_the_table_store_from_the_declared_variable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import omnimarket.handlers.floor_fill_state_store_postgres as pg

    class RecordingStore:
        def __init__(self, settings: Any) -> None:
            self._settings = settings
            self._rows: dict[str, tuple[str, int]] = {}

        def load(self, key: str) -> ModelStateRead:
            stored = self._rows.get(key)
            if stored is None:
                return ModelStateRead(payload=None, version=0)
            return ModelStateRead(payload=stored[0], version=stored[1])

        def compare_and_set(
            self, key: str, expected_version: int, new_version: int, payload: str
        ) -> bool:
            stored = self._rows.get(key)
            current = stored[1] if stored is not None else 0
            if current != expected_version:
                return False
            self._rows[key] = (payload, new_version)
            return True

    captured: list[Any] = []

    class FakePostgresStore(RecordingStore):
        def __init__(self, settings: Any) -> None:
            super().__init__(settings)
            captured.append(settings)

    monkeypatch.setattr(pg, "PostgresFloorStateStore", FakePostgresStore)
    handler = HandlerFloorFillScheduled(environ=environ(tmp_path))
    event = handler.handle(tick(T0))
    assert event is not None
    assert event.detail.startswith("UNKNOWN floor-unread")
    assert len(captured) == 1
    settings = captured[0]
    assert (
        settings.dsn.get_secret_value()
        == "postgresql://fixture-user:SENTINEL-PW@fixture-host/db"
    )
    assert settings.schema_name == "public"
    assert settings.table_name == "floor_fill_state"


_PROFILES = typing.get_args(
    ModelFloorFillScheduleOverlay.model_fields["profile"].annotation
)


@pytest.mark.parametrize("profile", _PROFILES)
def test_seam_every_profile_the_overlay_admits_is_bound_by_the_tick(
    tmp_path: Path, profile: str
) -> None:
    """A profile the schedule overlay admits and the tick does not bind raises FloorFillProfileUnboundError."""
    store = InMemoryFloorStateStore() if profile == "live" else None
    outcome = run_floor_fill_tick(
        fill_overlay_id="node_floor_fill_orchestrator",
        profile=profile,
        environ=environ(tmp_path),
        store=store,
    )
    assert outcome.first_line.startswith("UNKNOWN")


def test_seam_the_store_settings_come_from_the_overlay_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import omnimarket.handlers.floor_fill_state_store_postgres as pg

    class RecordingStore:
        def __init__(self, settings: Any) -> None:
            self._settings = settings
            self._rows: dict[str, tuple[str, int]] = {}

        def load(self, key: str) -> ModelStateRead:
            stored = self._rows.get(key)
            if stored is None:
                return ModelStateRead(payload=None, version=0)
            return ModelStateRead(payload=stored[0], version=stored[1])

        def compare_and_set(
            self, key: str, expected_version: int, new_version: int, payload: str
        ) -> bool:
            stored = self._rows.get(key)
            current = stored[1] if stored is not None else 0
            if current != expected_version:
                return False
            self._rows[key] = (payload, new_version)
            return True

    captured: list[Any] = []

    class FakePostgresStore(RecordingStore):
        def __init__(self, settings: Any) -> None:
            super().__init__(settings)
            captured.append(settings)

    monkeypatch.setattr(pg, "PostgresFloorStateStore", FakePostgresStore)
    handler = HandlerFloorFillScheduled(environ=environ(tmp_path))
    handler.handle(tick(T0))
    assert len(captured) == 1
    settings = captured[0]
    live = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))["floor_fill_live"]
    assert settings.schema_name == live["store_schema_name"]
    assert settings.table_name == live["store_table_name"]
    assert settings.connect_timeout_seconds == live["store_connect_timeout_seconds"]
