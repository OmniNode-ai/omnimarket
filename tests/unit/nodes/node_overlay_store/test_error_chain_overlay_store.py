# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Error chain: each named break ends in its own reading; a held snapshot survives every one of them."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.models.overlay_store import (
    EnumOverlayReadSource,
    EnumOverlayReadState,
    ModelOverlayStoreReadOverlay,
    ModelOverlayStoreRecord,
    document_digest,
)
from omnimarket.nodes.node_overlay_store_load_effect.handlers import (
    HandlerOverlayStoreLoad,
    OverlayHomeSource,
    OverlayLoadRefusedError,
)
from omnimarket.nodes.node_overlay_store_read_effect import (
    runtime_overlay_store_tick as tick_module,
)
from omnimarket.nodes.node_overlay_store_read_effect.handlers import (
    OverlayAbsentError,
    OverlayStaleError,
    compare_documents,
)
from omnimarket.nodes.node_overlay_store_read_effect.runtime_overlay_store_read import (
    EXIT_UNREADABLE,
    build_handler,
)
from omnimarket.nodes.node_overlay_store_read_effect.runtime_overlay_store_tick import (
    main as tick_main,
)
from tests.unit.nodes.node_overlay_store.support import (
    LOADED_TOPIC,
    TABLE,
    FakeBus,
    Reporter,
    Store,
    overlay_home,
    past_bound,
    reader,
    record,
)

pytestmark = pytest.mark.unit


def _loader(
    tmp_path: Path, table: dict | None = None
) -> tuple[HandlerOverlayStoreLoad, FakeBus]:
    bus = FakeBus()
    return (
        HandlerOverlayStoreLoad(
            source=OverlayHomeSource(overlay_home(tmp_path, table or TABLE)),
            bus=bus,
            topic=LOADED_TOPIC,
            loaded_by="test",
        ),
        bus,
    )


async def _held(tmp_path: Path) -> bytes:
    """A host holding a good snapshot, its bytes."""
    await reader(tmp_path, Store(record().model_dump(mode="json"))).handle("lab_hosts")
    return (tmp_path / "snapshot" / "lab_hosts" / "overlay.yaml").read_bytes()


@pytest.mark.asyncio
async def test_a_document_its_model_refuses_is_not_published(tmp_path: Path) -> None:
    broken = {**TABLE, "hosts": [{**TABLE["hosts"][0], "unknown_column": 1}]}
    loader, bus = _loader(tmp_path, broken)
    with pytest.raises(OverlayLoadRefusedError, match="unknown_column"):
        await loader.handle("lab_hosts")
    assert bus.published == []


@pytest.mark.asyncio
async def test_a_host_row_carrying_volumes_root_is_loaded_and_published(
    tmp_path: Path,
) -> None:
    """The lab_hosts overlay names a host's external volumes root (hosts.1.volumes_root on overlays main);
    the loader's model takes it, so the store moves to the new version instead of refusing it (OMN-23021)."""
    table = {
        **TABLE,
        "hosts": [TABLE["hosts"][0], {**TABLE["hosts"][1], "volumes_root": "/Volumes"}],
    }
    loader, bus = _loader(tmp_path, table)
    result = await loader.handle("lab_hosts")
    assert result.digest == document_digest(table)
    assert [topic for topic, _key, _value in bus.published] == [LOADED_TOPIC]


@pytest.mark.asyncio
async def test_a_volumes_root_at_the_filesystem_root_is_refused(tmp_path: Path) -> None:
    table = {
        **TABLE,
        "hosts": [TABLE["hosts"][0], {**TABLE["hosts"][1], "volumes_root": "/"}],
    }
    loader, bus = _loader(tmp_path, table)
    with pytest.raises(OverlayLoadRefusedError, match=r"hosts\.1\.volumes_root"):
        await loader.handle("lab_hosts")
    assert bus.published == []


@pytest.mark.asyncio
async def test_an_empty_table_is_not_published(tmp_path: Path) -> None:
    loader, bus = _loader(tmp_path, {**TABLE, "hosts": []})
    with pytest.raises(OverlayLoadRefusedError, match="hosts"):
        await loader.handle("lab_hosts")
    assert bus.published == []


@pytest.mark.asyncio
async def test_an_id_with_no_pin_line_is_refused(tmp_path: Path) -> None:
    loader, bus = _loader(tmp_path)
    unpinned = tmp_path / "home" / "overlays" / "not_pinned"
    unpinned.mkdir()
    (unpinned / "overlay.yaml").write_text("a: 1\n", encoding="utf-8")
    with pytest.raises(OverlayLoadRefusedError, match="no line"):
        await loader.handle("not_pinned")
    assert bus.published == []


def test_a_pin_line_may_only_name_a_model_of_this_package(tmp_path: Path) -> None:
    source = OverlayHomeSource(overlay_home(tmp_path))
    with pytest.raises(OverlayLoadRefusedError, match="not a model of"):
        source.model_of("outside")


def test_a_record_whose_digest_is_not_its_documents_is_refused() -> None:
    fields = record().model_dump(mode="json")
    with pytest.raises(ValidationError, match="digest"):
        ModelOverlayStoreRecord.model_validate({**fields, "digest": "0" * 64})


@pytest.mark.asyncio
async def test_store_down_keeps_the_snapshot_and_says_so(tmp_path: Path) -> None:
    held = await _held(tmp_path)
    reporter = Reporter()
    reading = await reader(tmp_path, Store(down=True), reporter=reporter).handle(
        "lab_hosts"
    )
    assert reading.state is EnumOverlayReadState.FRESH
    assert reading.source is EnumOverlayReadSource.SNAPSHOT
    assert reading.store_error is not None
    assert "broker refused" in reading.store_error
    assert (tmp_path / "snapshot" / "lab_hosts" / "overlay.yaml").read_bytes() == held
    assert reporter.readings == [reading]


@pytest.mark.asyncio
async def test_an_empty_store_keeps_the_snapshot_and_says_so(tmp_path: Path) -> None:
    held = await _held(tmp_path)
    reading = await reader(tmp_path, Store(None)).handle("lab_hosts")
    assert reading.source is EnumOverlayReadSource.SNAPSHOT
    assert reading.store_error == "the store holds no overlay lab_hosts"
    assert (tmp_path / "snapshot" / "lab_hosts" / "overlay.yaml").read_bytes() == held


@pytest.mark.asyncio
async def test_an_invalid_answer_keeps_the_snapshot_and_says_so(tmp_path: Path) -> None:
    held = await _held(tmp_path)
    bad = {**record().model_dump(mode="json"), "digest": "0" * 64}
    reading = await reader(tmp_path, Store(bad)).handle("lab_hosts")
    assert reading.store_error is not None
    assert "invalid" in reading.store_error
    assert (tmp_path / "snapshot" / "lab_hosts" / "overlay.yaml").read_bytes() == held


@pytest.mark.asyncio
async def test_past_the_bound_the_table_is_still_read_and_says_stale(
    tmp_path: Path,
) -> None:
    await _held(tmp_path)
    handler = reader(tmp_path, Store(down=True), now=past_bound())
    reading = handler.read("lab_hosts")
    assert reading.state is EnumOverlayReadState.STALE
    assert "over the 3600s bound" in reading.detail
    read = handler.read_document("lab_hosts")
    assert read.reading.state is EnumOverlayReadState.STALE
    assert [row["name"] for row in read.document["hosts"]] == ["h1", "h2"]


@pytest.mark.asyncio
async def test_a_refusing_policy_refuses_past_the_bound(tmp_path: Path) -> None:
    await _held(tmp_path)
    handler = reader(tmp_path, Store(down=True), now=past_bound(), on_stale="refuse")
    with pytest.raises(OverlayStaleError, match="over the 3600s bound"):
        handler.read_document("lab_hosts")


def test_no_snapshot_is_unreadable_and_a_read_refuses(tmp_path: Path) -> None:
    handler = reader(tmp_path, Store(None))
    reading = handler.read("lab_hosts")
    assert reading.state is EnumOverlayReadState.UNREADABLE
    assert "no snapshot" in reading.detail
    with pytest.raises(OverlayAbsentError):
        handler.read_document("lab_hosts")


@pytest.mark.asyncio
async def test_a_changed_snapshot_file_is_unreadable(tmp_path: Path) -> None:
    await _held(tmp_path)
    path = tmp_path / "snapshot" / "lab_hosts" / "overlay.yaml"
    path.write_text(path.read_text().replace("cap: 12", "cap: 99"), encoding="utf-8")
    reading = reader(tmp_path, Store(None)).read("lab_hosts")
    assert reading.state is EnumOverlayReadState.UNREADABLE
    assert "stamp" in reading.detail


@pytest.mark.asyncio
async def test_a_reporter_that_fails_does_not_fail_the_read(tmp_path: Path) -> None:
    reading = await reader(
        tmp_path,
        Store(record().model_dump(mode="json")),
        reporter=Reporter(fail=True),
    ).handle("lab_hosts")
    assert reading.state is EnumOverlayReadState.FRESH


def test_compare_names_every_differing_path() -> None:
    changed = {
        **TABLE,
        "bus_lane": "other",
        "hosts": [
            {**TABLE["hosts"][0], "cap": 13, "added": True},
        ],
    }
    compared = compare_documents(TABLE, changed)
    assert not compared.equal
    assert set(compared.differing) == {
        "bus_lane",
        "hosts.<row order or names>",
        "hosts.h1.added",
        "hosts.h1.cap",
    }


def test_compare_of_a_table_with_itself_counts_its_rows() -> None:
    compared = compare_documents(TABLE, TABLE)
    assert compared.equal
    assert compared.rows_compared == 2


def test_a_bus_snapshot_reader_with_no_broker_named_is_refused_at_load() -> None:
    with pytest.raises(ValidationError, match="bus_snapshot"):
        ModelOverlayStoreReadOverlay(
            backend="bus_snapshot",
            snapshot_root="root",
            read_window_s=1,
            host_name_env="HOST",
            overlays={"lab_hosts": {"stale_after_s": 1, "on_stale": "read"}},
        )


def test_a_host_never_selects_the_postgres_backend() -> None:
    overlay = ModelOverlayStoreReadOverlay(
        backend="postgres",
        database_url_env="URL",
        snapshot_root="root",
        read_window_s=1,
        host_name_env="HOST",
        overlays={"lab_hosts": {"stale_after_s": 1, "on_stale": "read"}},
    )
    with pytest.raises(NodeOverlayError, match="belongs to a runtime"):
        build_handler(overlay, {"HOST": "h1"})


def _tick_overlay(tmp_path: Path, **extra: str) -> dict[str, str]:
    node = tmp_path / "overlays" / "node_overlay_store_read_effect"
    node.mkdir(parents=True)
    lines = [
        "backend: in_memory",
        f"snapshot_root: {tmp_path / 'snapshot'}",
        "read_window_s: 1",
        "host_name_env: HOST",
        "overlays:",
        "  lab_hosts: {stale_after_s: 60, on_stale: read}",
        *(f"{key}: {value}" for key, value in extra.items()),
    ]
    (node / "overlay.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {
        "ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path / "overlays"),
        "HOST": "h1",
    }


def test_a_tick_with_no_usable_snapshot_exits_unreadable_and_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for key, value in _tick_overlay(tmp_path).items():
        monkeypatch.setenv(key, value)
    state = tmp_path / "state"
    code = tick_main(["--omni-home", str(tmp_path), "--state-dir", str(state)])
    assert code == EXIT_UNREADABLE
    run = json.loads((state / "last-run.json").read_text(encoding="utf-8"))
    assert run["readings"][0]["state"] == "unreadable"
    assert run["readings"][0]["store_error"] == "the store holds no overlay lab_hosts"


@pytest.mark.asyncio
async def test_a_tick_keeps_a_held_snapshot_when_the_store_holds_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for key, value in _tick_overlay(tmp_path).items():
        monkeypatch.setenv(key, value)
    held = reader(
        tmp_path, Store(record().model_dump(mode="json")), reporter=Reporter()
    )
    await held.handle("lab_hosts")
    written = tmp_path / "snapshot" / "lab_hosts" / "overlay.yaml"
    before = written.read_bytes()
    state = tmp_path / "state"
    code = await asyncio.to_thread(
        tick_main, ["--omni-home", str(tmp_path), "--state-dir", str(state)]
    )
    assert code == 0
    assert written.read_bytes() == before
    run = json.loads((state / "last-run.json").read_text(encoding="utf-8"))
    assert run["readings"][0]["source"] == "snapshot"
    assert run["readings"][0]["store_error"] == "the store holds no overlay lab_hosts"


def test_a_tick_with_the_broker_down_still_records_this_hosts_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for key, value in _tick_overlay(
        tmp_path, fleet_record_relpath="state/fleet.json"
    ).items():
        monkeypatch.setenv(key, value)
    held = tmp_path / "state" / "fleet.json"
    held.parent.mkdir()
    held.write_text(
        json.dumps({"readings": {"h2:lab_hosts": {"host": "h2", "state": "fresh"}}}),
        encoding="utf-8",
    )

    async def refused(*_: object) -> dict[str, object]:
        raise ConnectionError("broker down")

    monkeypatch.setattr(tick_module, "fleet", refused)
    tick_main(["--omni-home", str(tmp_path), "--state-dir", str(tmp_path / "run")])
    readings = json.loads(held.read_text(encoding="utf-8"))["readings"]
    assert readings["h2:lab_hosts"]["state"] == "fresh"
    assert readings["h1:lab_hosts"]["state"] == "unreadable"


def test_a_tick_arms_the_contracts_deadline_before_it_touches_the_bus(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for key, value in _tick_overlay(tmp_path).items():
        monkeypatch.setenv(key, value)
    armed: list[tuple[float, bool]] = []
    monkeypatch.setattr(
        tick_module.faulthandler,
        "dump_traceback_later",
        lambda seconds, exit: armed.append((seconds, exit)),
    )
    tick_main(["--omni-home", str(tmp_path), "--state-dir", str(tmp_path / "run")])
    assert armed == [(120.0, True)]
    assert tick_module.tick_deadline_s() == 120.0
