# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A merged overlay change reaches a host's snapshot when overlays main moves past the snapshot's source sha (OMN-26432).

Recorded case, 2026-11-22: overlays#775 merged 02:46:43Z (f39ded4694). The loader published the record at 03:06:20Z,
and h201 took it at 03:13:17Z and the Mac at 03:16:13Z, while each tick in between printed the old digest as a fresh
snapshot. Two things held it back, and each test names the one it covers:

* the loader's unit ran once an hour and published on every run, so a tick could not be made frequent without growing
  the topic, and a merge waited for the next hourly run (or a hand start);
* a reader on a stable consumer group takes what one window delivers and reports ``fresh`` while overlays main is
  ahead of its snapshot, so a slow link needed one tick per window to reach the record.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from omnimarket.models.overlay_store import (
    EnumOverlayReadSource,
    EnumOverlayReadState,
    ModelOverlayStoreReading,
    ModelOverlayStoreReadOverlay,
    ModelOverlayStoreRecord,
    document_digest,
)
from omnimarket.nodes.node_overlay_store_load_effect import (
    runtime_overlay_store_load_tick as load_tick,
)
from omnimarket.nodes.node_overlay_store_read_effect import (
    runtime_overlay_store_read as read,
)
from tests.unit.nodes.node_overlay_store.support import TABLE
from tests.unit.nodes.node_overlay_store.test_overlay_store_load_tick import (
    OVERLAY,
    fake_run,
    git,
    last_run,
)

pytestmark = pytest.mark.unit

VERSIONS = 14
PER_WINDOW = 4
FIRST = datetime.now(UTC) - timedelta(hours=1)


# --- the loader --------------------------------------------------------------------------------------------------


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A registry root whose ``overlays`` clone sits at origin/main, and the loader's own overlay found in it."""
    root = tmp_path / "omni"
    clone = root / "overlays"
    (clone / "overlays" / "node_overlay_store_load_effect").mkdir(parents=True)
    (clone / "overlays" / "node_overlay_store_load_effect" / "overlay.yaml").write_text(
        OVERLAY, encoding="utf-8"
    )
    git(clone, "init", "-q", "-b", "main")
    git(clone, "add", "-A")
    git(clone, "commit", "-q", "-m", "x")
    git(clone, "update-ref", "refs/remotes/origin/main", "HEAD")
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(clone / "overlays"))
    return root


def _tick(home: Path, state: Path) -> int:
    return load_tick.main(["--omni-home", str(home), "--state-dir", str(state)])


def _merge(clone: Path, name: str) -> str:
    """A merge to overlays main: a commit that origin/main then names."""
    (clone / f"{name}.txt").write_text(name, encoding="utf-8")
    git(clone, "add", "-A")
    git(clone, "commit", "-q", "-m", name)
    git(clone, "update-ref", "refs/remotes/origin/main", "HEAD")
    return git(clone, "rev-parse", "HEAD")


def test_overlay_store_reload_on_source_move_loader_publishes_on_a_move_and_not_on_every_tick(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    loaded: list[str] = []
    monkeypatch.setattr(load_tick, "_run", fake_run(loaded))
    state = tmp_path / "state"
    assert _tick(home, state) == 0
    assert _tick(home, state) == 0
    assert loaded == ["lab_hosts"], (
        "a second tick with overlays main where the last load left it published again"
    )
    moved = _merge(home / "overlays", "merge-775")
    assert _tick(home, state) == 0
    assert loaded == ["lab_hosts", "lab_hosts"]
    assert last_run(state)["loaded"][0]["source_ref"] == f"lab_hosts@{moved}"


def test_overlay_store_reload_on_source_move_an_unchanged_tick_says_what_it_skipped(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    loaded: list[str] = []
    monkeypatch.setattr(load_tick, "_run", fake_run(loaded))
    state = tmp_path / "state"
    _tick(home, state)
    _tick(home, state)
    record = last_run(state)
    head = git(home / "overlays", "rev-parse", "HEAD")
    assert record["loaded"] == []
    assert record["unchanged"] == [f"lab_hosts@{head}"]
    assert record["refused"] == ""


def test_the_loader_asserts_an_unchanged_overlay_again_once_the_reassert_bound_has_passed(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The loader's time is what a reader's staleness is measured against, so an unchanged overlay is still asserted."""
    loaded: list[str] = []
    monkeypatch.setattr(load_tick, "_run", fake_run(loaded))
    state = tmp_path / "state"
    now = datetime(2026, 11, 22, 3, 0, tzinfo=UTC)
    monkeypatch.setattr(load_tick, "_utc_now", lambda: now)
    _tick(home, state)
    monkeypatch.setattr(
        load_tick,
        "_utc_now",
        lambda: now + timedelta(seconds=load_tick.REASSERT_AFTER_S - 1),
    )
    _tick(home, state)
    assert loaded == ["lab_hosts"]
    monkeypatch.setattr(
        load_tick,
        "_utc_now",
        lambda: now + timedelta(seconds=load_tick.REASSERT_AFTER_S + 1),
    )
    _tick(home, state)
    assert loaded == ["lab_hosts", "lab_hosts"]


def test_a_load_that_did_not_publish_is_tried_again_on_the_next_tick(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broker_down(*_: Any) -> list[Any]:
        raise ConnectionError("broker refused")

    state = tmp_path / "state"
    monkeypatch.setattr(load_tick, "_run", broker_down)
    assert _tick(home, state) == load_tick.EXIT_BUS
    loaded: list[str] = []
    monkeypatch.setattr(load_tick, "_run", fake_run(loaded))
    assert _tick(home, state) == 0
    assert loaded == ["lab_hosts"]


# --- the reader --------------------------------------------------------------------------------------------------


def _version(n: int, head: str) -> ModelOverlayStoreRecord:
    """Version ``n`` of 14; the last one is the record of overlays main."""
    table = {**TABLE, "bus_lane": f"lane-{n}"}
    return ModelOverlayStoreRecord(
        overlay_id="lab_hosts",
        digest=document_digest(table),
        document=table,
        source_ref=f"lab_hosts@{head if n == VERSIONS - 1 else f'old{n}'}",
        loaded_by="test",
        loaded_at=FIRST + timedelta(minutes=n),
    )


class SlowLinkBus:
    """A log of versions, a group's committed position, and a window that ends after ``PER_WINDOW`` records."""

    def __init__(self, head: str, *, published: int = VERSIONS) -> None:
        self.log = [
            _version(n, head).model_dump_json().encode() for n in range(published)
        ]
        self.positions: dict[str, int] = {}
        self.groups: list[str] = []

    async def publish(
        self, topic: str, key: bytes | None, value: bytes, headers: Any = None
    ) -> None:
        return None

    async def subscribe(
        self,
        topic: str,
        on_message: Callable[[Any], Awaitable[None]],
        group_id: str,
        auto_offset_reset: str = "latest",
    ) -> Callable[[], Awaitable[None]]:
        self.groups.append(group_id)
        start = self.positions.get(group_id, 0)
        taken = self.log[start : start + PER_WINDOW]

        class Message:
            def __init__(self, value: bytes) -> None:
                self.value = value

        for value in taken:
            await on_message(Message(value))
        committed = start + len(taken)

        async def stop() -> None:
            self.positions[group_id] = committed

        return stop


def _env(tmp_path: Path) -> tuple[dict[str, str], str]:
    """A host's unit environment with its overlays clone at origin/main, and the sha of that main."""
    clone = tmp_path / "overlays-clone"
    (clone / "overlays" / "lab_hosts").mkdir(parents=True)
    (clone / "overlays" / "lab_hosts" / "overlay.yaml").write_text(
        "x: 1\n", encoding="utf-8"
    )
    git(clone, "init", "-q", "-b", "main")
    git(clone, "add", "-A")
    git(clone, "commit", "-q", "-m", "x")
    git(clone, "update-ref", "refs/remotes/origin/main", "HEAD")
    return (
        {"HOST": "h105", "ONEX_SKILL_OVERLAY_ROOTS": str(clone / "overlays")},
        git(clone, "rev-parse", "HEAD"),
    )


def _read_tick(
    monkeypatch: pytest.MonkeyPatch,
    bus: SlowLinkBus,
    snapshot_root: Path,
    env: dict[str, str],
) -> ModelOverlayStoreReading:
    @contextlib.asynccontextmanager
    async def opened() -> AsyncIterator[SlowLinkBus]:
        yield bus

    monkeypatch.setattr(read, "open_lane_bus", lambda **_: opened())
    overlay = ModelOverlayStoreReadOverlay.model_validate(
        {
            "backend": "bus_snapshot",
            "bus_lane": "store-lane",
            "snapshot_root": str(snapshot_root),
            "read_window_s": 0.05,
            "host_name_env": "HOST",
            "overlays": {"lab_hosts": {"stale_after_s": 21600, "on_stale": "read"}},
        }
    )
    handler = read.build_handler(overlay, env)
    return asyncio.run(handler.refresh_all())[0]


def test_overlay_store_reload_on_source_move_one_tick_takes_the_record_of_main_past_a_slow_window(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    env, head = _env(tmp_path)
    bus = SlowLinkBus(head)
    snapshot = tmp_path / "snap"
    first = _read_tick(monkeypatch, bus, snapshot, env)
    assert first.source is EnumOverlayReadSource.STORE
    second = _read_tick(monkeypatch, bus, snapshot, env)
    newest = _version(VERSIONS - 1, head)
    assert second.digest == newest.digest, (
        "overlays main is ahead of the snapshot, and the tick stopped at what one window delivered"
    )
    assert second.source is EnumOverlayReadSource.STORE
    stamp = json.loads(
        (snapshot / "lab_hosts" / "overlay.yaml.stamp.json").read_text("utf-8")
    )
    assert stamp["source_ref"] == f"lab_hosts@{head}"


def test_overlay_store_reload_on_source_move_a_snapshot_behind_main_says_so_when_the_store_has_no_record_yet(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    env, head = _env(tmp_path)
    bus = SlowLinkBus(head, published=VERSIONS - 1)  # the loader has not published main
    snapshot = tmp_path / "snap"
    for _ in range(6):
        reading = _read_tick(monkeypatch, bus, snapshot, env)
    assert reading.state is EnumOverlayReadState.FRESH
    assert head in reading.detail
    assert "old" in reading.detail


def test_a_snapshot_at_main_takes_one_window_and_says_nothing_of_a_move(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    env, head = _env(tmp_path)
    bus = SlowLinkBus(head)
    snapshot = tmp_path / "snap"
    for _ in range(6):
        reading = _read_tick(monkeypatch, bus, snapshot, env)
    assert reading.digest == _version(VERSIONS - 1, head).digest
    assert reading.detail == ""
    before = len(bus.groups)
    _read_tick(monkeypatch, bus, snapshot, env)
    assert len(bus.groups) == before + 1


def test_a_host_with_no_overlays_clone_reads_as_it_did(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _, head = _env(tmp_path)
    bus = SlowLinkBus(head)
    reading = _read_tick(monkeypatch, bus, tmp_path / "snap", {"HOST": "h105"})
    assert reading.digest == _version(PER_WINDOW - 1, head).digest
    assert reading.detail == ""
