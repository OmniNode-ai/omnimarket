# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The panel reducer through the runtime's production state_io wrapper, over an injected versioned store (OMN-19998).

``test_concurrent_cas`` and ``test_cas_exhaustion_leaves_row`` drive omnibase_infra's own
``_make_stateful_dispatch_callback`` (load, handle, save with the version compare, retry on a lost compare) and the node's own
codec; only the database adapter is replaced by an in-memory versioned store. Each check returns what is wrong; each control is
a named injected break that must make the same check report a problem, so a pass is never a check unable to fail.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import omnibase_infra.runtime.auto_wiring.handler_wiring as wiring
import pytest
import yaml
from omnibase_infra.utils.util_retry_optimistic import OptimisticConflictError

from omnimarket.models.work_state_panel import (
    ModelWorkStatePanelEntry,
    ModelWorkStatePanelOverlay,
)
from omnimarket.models.work_state_projection.model_work_state_projected_event import (
    EnumWorkStateProjectorProfile,
    ModelWorkStateProjectedEvent,
)
from omnimarket.nodes.node_work_state_panel_reducer.handlers import (
    handler_work_state_panel_reducer as node_handler_module,
)
from omnimarket.nodes.node_work_state_panel_reducer.handlers.handler_work_state_panel_reducer import (
    HandlerWorkStatePanelReducer,
)
from omnimarket.nodes.node_work_state_panel_reducer.state_codec import (
    reset_default_proxy,
)

pytestmark = pytest.mark.unit

T0 = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)
NOW = T0 + timedelta(seconds=600)
TOPIC = "onex.evt.omnimarket.work-state-projected.v1"
CONTRACT = Path(node_handler_module.__file__).resolve().parent.parent / "contract.yaml"
DB_URL_ENV = "OMNIBASE_INFRA_DB_URL"


def overlay() -> ModelWorkStatePanelOverlay:
    return ModelWorkStatePanelOverlay(
        panels=(
            ModelWorkStatePanelEntry(
                panel_id="work_state_latest_run",
                projector=EnumWorkStateProjectorProfile.FILE_SOURCES,
                topic=TOPIC,
                max_age_s=900,
                max_future_skew_s=60,
                accept_deadline_s=600,
                audience="team_service",
                allowed_readers=("reader-a",),
                answer_writer="writer-a",
                reducer_group="panel-reducer",
                start_position="latest",
            ),
        )
    )


def wire(as_of_s: int) -> dict[str, Any]:
    return ModelWorkStateProjectedEvent(
        computed_at=T0,
        tick_id="t1",
        projector=EnumWorkStateProjectorProfile.FILE_SOURCES,
        as_of=T0 + timedelta(seconds=as_of_s),
        item_count=1,
        counts={"done": 1},
        ready_in_scope=0,
    ).model_dump(mode="json")


def envelope(as_of_s: int) -> dict[str, Any]:
    return {"payload": wire(as_of_s), "__debug_trace": {"topic": TOPIC}}


class UnusedPool:
    """The pool provider the wrapper builds; the injected store never opens a connection."""

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        pass

    def create(self, *_args: object, **_kwargs: object) -> object:
        raise AssertionError("the injected store opened a pool")


class VersionedStore:
    """The state store's adapter surface the wrapper uses, in memory, with a save log and a compare that can be broken."""

    def __init__(self) -> None:
        self.rows: dict[str, tuple[str, int]] = {}
        self.save_log: list[int] = []
        self.version_check = True
        self.stale_reload = False
        self.ghost_write_on_lost_compare = False
        self.always_lose = False
        self.load_gate: asyncio.Barrier | None = None
        self.gate_waits_left = 0
        self.loser_as_of: int | None = None
        self.winner_saved = asyncio.Event()
        self._first_loaded: dict[str, tuple[str, int] | None] = {}

    def __call__(self, *_args: object, **_kwargs: object) -> VersionedStore:
        return self

    async def recover_stale_rows(self) -> int:
        return 0

    async def load(self, cid: str) -> tuple[str, int] | None:
        row = self.rows.get(cid)
        if self.stale_reload:
            row = self._first_loaded.setdefault(cid, row)
        if self.load_gate is not None and self.gate_waits_left > 0:
            self.gate_waits_left -= 1
            await self.load_gate.wait()
        return row

    @staticmethod
    def _as_of_seconds(payload_json: str) -> int:
        as_of = datetime.fromisoformat(json.loads(payload_json)["as_of"])
        return int((as_of - T0).total_seconds())

    async def _before_save(self, payload_json: str) -> None:
        if self.loser_as_of is None:
            return
        if self._as_of_seconds(payload_json) == self.loser_as_of:
            await self.winner_saved.wait()

    def _after_save(self, payload_json: str) -> None:
        if (
            self.loser_as_of is not None
            and self._as_of_seconds(payload_json) != self.loser_as_of
        ):
            self.winner_saved.set()

    async def seed(self, cid: str, *, payload_json: str, **_kw: object) -> bool:
        await self._before_save(payload_json)
        if cid in self.rows:
            return False
        self.rows[cid] = (payload_json, 0)
        self.save_log.append(0)
        self._after_save(payload_json)
        return True

    async def cas_update(
        self, cid: str, *, expected_version: int, payload_json: str, **_kw: object
    ) -> int:
        await self._before_save(payload_json)
        current = self.rows.get(cid)
        lost = (
            self.always_lose
            or current is None
            or (self.version_check and current[1] != expected_version)
        )
        if lost:
            if self.ghost_write_on_lost_compare:
                self.rows[cid] = (payload_json, expected_version + 1)
            self._after_save(payload_json)
            return 0
        version = (current[1] if current else expected_version) + 1
        self.rows[cid] = (payload_json, version)
        self.save_log.append(version)
        self._after_save(payload_json)
        return 1


def build(monkeypatch: pytest.MonkeyPatch, store: VersionedStore) -> Any:
    """The production wrapper around the node's own handler and codec, over the injected store."""
    monkeypatch.setattr(wiring, "StateStoreAdapter", store)
    monkeypatch.setattr(wiring, "ProviderPostgresPool", UnusedPool)
    monkeypatch.setenv(DB_URL_ENV, "postgresql://user:pw@host/db")
    reset_default_proxy()
    contract = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    handler = HandlerWorkStatePanelReducer(clock=lambda: NOW, overlay=overlay())
    return wiring._make_stateful_dispatch_callback(handler, None, contract["state_io"])


def held(store: VersionedStore) -> dict[str, Any]:
    return json.loads(store.rows["file_sources"][0])


async def race(
    monkeypatch: pytest.MonkeyPatch, store: VersionedStore, winner: int, loser: int
) -> None:
    """Preload as_of 50, then two dispatches both load that version before either saves; the loser's save waits for the winner's."""
    callback = build(monkeypatch, store)
    await callback(envelope(50))
    store.save_log.clear()
    store.winner_saved = asyncio.Event()
    store.loser_as_of = loser
    store.load_gate = asyncio.Barrier(2)
    store.gate_waits_left = 2
    await asyncio.gather(callback(envelope(winner)), callback(envelope(loser)))


def problems_concurrent_cas(
    store: VersionedStore, *, preloaded_epoch: int = 1
) -> list[str]:
    problems: list[str] = []
    row = held(store)
    if row["as_of"] != "2026-11-14T12:03:20Z":
        problems.append(f"held as_of is {row['as_of']}, not the newest (200 s)")
    if row["epoch"] != preloaded_epoch + len(store.save_log):
        problems.append(
            f"epoch {row['epoch']} is not the preloaded {preloaded_epoch} plus the {len(store.save_log)} committed saves"
        )
    versions = store.save_log
    if (
        versions != list(range(versions[0], versions[0] + len(versions)))
        if versions
        else False
    ):
        problems.append(f"saved versions have a gap or a repeat: {versions}")
    return problems


@pytest.mark.parametrize(("winner", "loser"), [(200, 100), (100, 200)])
async def test_concurrent_cas(
    monkeypatch: pytest.MonkeyPatch, winner: int, loser: int
) -> None:
    store = VersionedStore()
    await race(monkeypatch, store, winner, loser)
    assert problems_concurrent_cas(store) == []


async def test_control_persist_without_version_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Injected break `persist_without_version_check`: the loser overwrites the newer row."""
    store = VersionedStore()
    store.version_check = False
    await race(monkeypatch, store, winner=200, loser=100)
    assert problems_concurrent_cas(store) != []


async def test_control_loser_never_reloads(monkeypatch: pytest.MonkeyPatch) -> None:
    """Injected break `reload_returns_the_first_read`: a retry that does not see the winner's row never wins its compare."""
    store = VersionedStore()
    store.stale_reload = True
    with pytest.raises(OptimisticConflictError):
        await race(monkeypatch, store, winner=100, loser=200)


def problems_cas_exhaustion(
    store: VersionedStore, before: tuple[str, int]
) -> list[str]:
    problems: list[str] = []
    if store.rows["file_sources"] != before:
        problems.append("the row changed although every compare was lost")
    if store.save_log:
        problems.append(f"the save log has entries {store.save_log}")
    return problems


async def test_cas_exhaustion_leaves_row(monkeypatch: pytest.MonkeyPatch) -> None:
    store = VersionedStore()
    callback = build(monkeypatch, store)
    await callback(envelope(50))
    before = store.rows["file_sources"]
    store.save_log.clear()
    store.always_lose = True
    with pytest.raises(OptimisticConflictError):
        await callback(envelope(100))
    assert problems_cas_exhaustion(store, before) == []
    store.always_lose = False
    await callback(envelope(100))
    assert held(store)["as_of"] == "2026-11-14T12:01:40Z"
    assert held(store)["epoch"] == 2


async def test_control_ghost_write_on_lost_compare(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Injected break `ghost_write_on_lost_compare`: a lost compare still changes the row."""
    store = VersionedStore()
    callback = build(monkeypatch, store)
    await callback(envelope(50))
    before = store.rows["file_sources"]
    store.save_log.clear()
    store.always_lose = True
    store.ghost_write_on_lost_compare = True
    with contextlib.suppress(OptimisticConflictError):
        await callback(envelope(100))
    assert problems_cas_exhaustion(store, before) != []
