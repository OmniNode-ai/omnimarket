# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Lane context directories on one host link one shared copy of each ledger archive roll (OMN-23297).

A lane's context directory holds its own ledger snapshot; the archive rolls in it are immutable and
the same for every lane, about 216 MB a lane. The real filesystem runs in ``tmp_path``: the
sharing is read as the inode of the roll in two context directories.
"""

from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path

import pytest

from omnimarket.nodes.node_lane_ctx_ledger_dedupe_effect.__main__ import main
from omnimarket.nodes.node_lane_ctx_ledger_dedupe_effect.handlers.adapter_fs import (
    HostLaneCtxFs,
    MemoryLaneCtxFs,
)
from omnimarket.nodes.node_lane_ctx_ledger_dedupe_effect.handlers.adapter_publisher import (
    RecordingPublisher,
    contract_topics,
)
from omnimarket.nodes.node_lane_ctx_ledger_dedupe_effect.handlers.handler_lane_ctx_ledger_dedupe import (
    HandlerLaneCtxLedgerDedupe,
)
from omnimarket.nodes.node_lane_ctx_ledger_dedupe_effect.models.model_lane_ctx_ledger_dedupe import (
    ModelLaneCtxLedgerDedupeCommand,
    ModelLaneCtxLedgerDedupeEvent,
    ModelLaneCtxLedgerDedupeResult,
)
from omnimarket.nodes.node_lane_ctx_ledger_dedupe_effect.protocols import (
    ProtocolLaneCtxFs,
)

ROLL = "ROLLING_WORK_LEDGER_2026-08-29-split.md"
OTHER = "ROLLING_WORK_LEDGER_2026-08-31-split.md"
NOW = 1_000_000.0
REFUSED, COMPLETED, _ = contract_topics()


def _handler(
    fs: ProtocolLaneCtxFs, publisher: RecordingPublisher, now: float = NOW
) -> HandlerLaneCtxLedgerDedupe:
    return HandlerLaneCtxLedgerDedupe(
        fs=fs,
        publisher=publisher,
        refused_topic=REFUSED,
        completed_topic=COMPLETED,
        clock=lambda: now,
    )


def _command(
    runs: str, store: str, *, execute: bool = True, settle: int = 60
) -> ModelLaneCtxLedgerDedupeCommand:
    return ModelLaneCtxLedgerDedupeCommand(
        runs_root=runs, store_dir=store, settle_seconds=settle, execute=execute
    )


def _make_ctx(
    runs: Path, run: str, rolls: dict[str, bytes], *, age: float = 3600
) -> list[Path]:
    archive = runs / f"{run}.ctx" / "ledger" / "archive"
    archive.mkdir(parents=True)
    paths = []
    for name, data in rolls.items():
        path = archive / name
        path.write_bytes(data)
        path.chmod(0o444)
        os.utime(path, (NOW - age, NOW - age))
        paths.append(path)
    return paths


def _inode(path: Path) -> tuple[int, int]:
    st = path.stat()
    return st.st_dev, st.st_ino


def _assert_shared(*paths: Path) -> None:
    """The check AC2 reads on a host: every path is the same inode."""
    assert len({_inode(p) for p in paths}) == 1, [str(_inode(p)) for p in paths]


# --- golden chain: the real filesystem -------------------------------------------------------


def test_ctx_ledger_dedupe_golden_two_lanes_share_one_inode(tmp_path: Path) -> None:
    runs, store = tmp_path / "runs", tmp_path / "store"
    runs.mkdir()
    data = b"row\n" * 4096
    a = _make_ctx(runs, "lane-a", {ROLL: data, OTHER: b"x" * 100})
    b = _make_ctx(runs, "lane-b", {ROLL: data, OTHER: b"x" * 100})
    assert _inode(a[0]) != _inode(b[0])  # before: each lane holds its own copy
    publisher = RecordingPublisher()

    result = _handler(HostLaneCtxFs(), publisher).handle(
        _command(str(runs), str(store))
    )

    _assert_shared(a[0], b[0], store / ROLL)
    _assert_shared(a[1], b[1], store / OTHER)
    assert (result.contexts, result.rolls_seen, result.stored, result.linked) == (
        2,
        4,
        2,
        2,
    )
    assert result.bytes_freed == len(data) + 100
    assert b[0].read_bytes() == data
    assert stat.S_IMODE(b[0].stat().st_mode) == 0o444  # the roll stays read-only
    assert b[0].stat().st_nlink == 3
    assert result.exit_code == 0
    assert result.errors == ()
    topics = [t for t, _ in publisher.events]
    assert topics == [COMPLETED]
    assert publisher.events[0][1] == result


def test_ctx_ledger_dedupe_golden_second_pass_changes_nothing(tmp_path: Path) -> None:
    runs, store = tmp_path / "runs", tmp_path / "store"
    runs.mkdir()
    a = _make_ctx(runs, "lane-a", {ROLL: b"abc"})
    b = _make_ctx(runs, "lane-b", {ROLL: b"abc"})
    command = _command(str(runs), str(store))
    _handler(HostLaneCtxFs(), RecordingPublisher()).handle(command)

    again = _handler(HostLaneCtxFs(), RecordingPublisher()).handle(command)

    assert (again.linked, again.stored, again.already_shared, again.bytes_freed) == (
        0,
        0,
        2,
        0,
    )
    _assert_shared(a[0], b[0])


def test_ctx_ledger_dedupe_removing_a_lane_directory_keeps_the_others(
    tmp_path: Path,
) -> None:
    runs, store = tmp_path / "runs", tmp_path / "store"
    runs.mkdir()
    a = _make_ctx(runs, "lane-a", {ROLL: b"abc"})
    b = _make_ctx(runs, "lane-b", {ROLL: b"abc"})
    _handler(HostLaneCtxFs(), RecordingPublisher()).handle(
        _command(str(runs), str(store))
    )
    shutil.rmtree(
        runs / "lane-a.ctx", ignore_errors=True
    )  # a lane's retention removes its directory
    assert a[0].exists() is False
    assert b[0].read_bytes() == b"abc"
    assert (store / ROLL).read_bytes() == b"abc"


def test_ctx_ledger_dedupe_dry_run_changes_nothing(tmp_path: Path) -> None:
    runs, store = tmp_path / "runs", tmp_path / "store"
    runs.mkdir()
    a = _make_ctx(runs, "lane-a", {ROLL: b"abc"})
    b = _make_ctx(runs, "lane-b", {ROLL: b"abc"})
    c = _make_ctx(runs, "lane-c", {ROLL: b"abc"})

    result = _handler(HostLaneCtxFs(), RecordingPublisher()).handle(
        _command(str(runs), str(store), execute=False)
    )

    assert result.mode == "dry-run"
    assert (result.stored, result.linked, result.bytes_freed) == (1, 2, 6)
    assert len({_inode(p) for p in (a[0], b[0], c[0])}) == 3
    assert not store.exists()


def test_ctx_ledger_dedupe_differing_bytes_stay_the_lanes_own(tmp_path: Path) -> None:
    runs, store = tmp_path / "runs", tmp_path / "store"
    runs.mkdir()
    a = _make_ctx(runs, "lane-a", {ROLL: b"abc"})
    b = _make_ctx(runs, "lane-b", {ROLL: b"abd"})  # same size, other bytes
    c = _make_ctx(runs, "lane-c", {ROLL: b"abcdef"})  # other size

    result = _handler(HostLaneCtxFs(), RecordingPublisher()).handle(
        _command(str(runs), str(store))
    )

    assert result.kept_own == 2
    assert result.linked == 0
    assert result.kept_own_rolls == (str(b[0]), str(c[0]))
    assert (b[0].read_bytes(), c[0].read_bytes()) == (b"abd", b"abcdef")
    assert _inode(a[0]) == _inode(store / ROLL)
    assert _inode(b[0]) != _inode(a[0])


def test_ctx_ledger_dedupe_leaves_a_roll_still_being_unpacked(tmp_path: Path) -> None:
    runs, store = tmp_path / "runs", tmp_path / "store"
    runs.mkdir()
    a = _make_ctx(runs, "lane-a", {ROLL: b"abc"})
    b = _make_ctx(
        runs, "lane-b", {ROLL: b"abc"}, age=5
    )  # modified 5 s ago, settle window 60 s

    result = _handler(HostLaneCtxFs(), RecordingPublisher()).handle(
        _command(str(runs), str(store))
    )

    assert (result.unsettled, result.linked, result.stored) == (1, 0, 1)
    assert _inode(b[0]) != _inode(a[0])


def test_ctx_ledger_dedupe_ignores_symlinked_rolls_and_foreign_directories(
    tmp_path: Path,
) -> None:
    runs, store = tmp_path / "runs", tmp_path / "store"
    runs.mkdir()
    (runs / "not-a-context").mkdir()
    a = _make_ctx(runs, "lane-a", {ROLL: b"abc"})
    link_ctx = runs / "lane-b.ctx" / "ledger" / "archive"
    link_ctx.mkdir(parents=True)
    (link_ctx / ROLL).symlink_to(a[0])

    result = _handler(HostLaneCtxFs(), RecordingPublisher()).handle(
        _command(str(runs), str(store))
    )

    assert result.contexts == 2
    assert result.rolls_seen == 1
    assert (link_ctx / ROLL).is_symlink()


# --- golden chain: the in-memory profile -----------------------------------------------------


def test_ctx_ledger_dedupe_golden_memory_profile() -> None:
    fs = MemoryLaneCtxFs()
    for lane in ("a", "b", "c"):
        fs.add_roll(f"/runs/{lane}.ctx/ledger/archive/{ROLL}", b"abc", NOW - 3600)
    publisher = RecordingPublisher()

    result = _handler(fs, publisher).handle(_command("/runs", "/store"))

    assert (result.stored, result.linked, result.bytes_freed) == (1, 2, 6)
    inodes = {
        fs.names[f"/runs/{lane}.ctx/ledger/archive/{ROLL}"].number for lane in "abc"
    }
    assert inodes == {fs.names[f"/store/{ROLL}"].number}
    assert [t for t, _ in publisher.events] == [COMPLETED]


# --- error chain -----------------------------------------------------------------------------


def test_ctx_ledger_dedupe_error_chain_cross_device_roll_is_refused_and_published() -> (
    None
):
    fs = MemoryLaneCtxFs(fail_link_new={f"/runs/a.ctx/ledger/archive/{ROLL}"})
    fs.add_roll(f"/runs/a.ctx/ledger/archive/{ROLL}", b"abc", NOW - 3600)
    fs.add_roll(f"/runs/b.ctx/ledger/archive/{ROLL}", b"abc", NOW - 3600)
    publisher = RecordingPublisher()

    result = _handler(fs, publisher).handle(_command("/runs", "/store"))

    assert result.exit_code == 1
    assert result.errors == (
        f"/runs/a.ctx/ledger/archive/{ROLL}: OSError: Invalid cross-device link",
    )
    refused = [e for t, e in publisher.events if t == REFUSED]
    assert refused == [
        ModelLaneCtxLedgerDedupeEvent(
            roll=f"/runs/a.ctx/ledger/archive/{ROLL}",
            reason="OSError: Invalid cross-device link",
        )
    ]
    assert publisher.events[-1][0] == COMPLETED
    done = publisher.events[-1][1]
    assert isinstance(done, ModelLaneCtxLedgerDedupeResult)
    assert done.errors == result.errors


def test_ctx_ledger_dedupe_error_chain_store_on_another_device_is_one_refusal() -> None:
    fs = MemoryLaneCtxFs(store_device=2)
    for lane in ("a", "b", "c"):
        fs.add_roll(f"/runs/{lane}.ctx/ledger/archive/{ROLL}", b"abc", NOW - 3600)
    publisher = RecordingPublisher()

    result = _handler(fs, publisher).handle(_command("/runs", "/store"))

    assert result.exit_code == 1
    assert (result.contexts, result.linked, result.stored) == (0, 0, 0)
    assert result.errors == (
        "/store: store_dir is on another device than runs_root: a hard link cannot cross it",
    )
    assert [t for t, _ in publisher.events] == [REFUSED, COMPLETED]
    assert f"/store/{ROLL}" not in fs.names


def test_ctx_ledger_dedupe_error_chain_unreadable_runs_root_is_refused(
    tmp_path: Path,
) -> None:
    publisher = RecordingPublisher()

    result = _handler(HostLaneCtxFs(), publisher).handle(
        _command(str(tmp_path / "missing"), str(tmp_path / "store"))
    )

    assert result.exit_code == 1
    assert result.contexts == 0
    assert [t for t, _ in publisher.events] == [REFUSED, COMPLETED]


def test_ctx_ledger_dedupe_event_topics_are_the_contracts() -> None:
    assert REFUSED == "onex.evt.omnimarket.lane-ctx-ledger-dedupe-refused.v1"
    assert COMPLETED == "onex.evt.omnimarket.lane-ctx-ledger-dedupe-completed.v1"


# --- failing control: the copy path restored -------------------------------------------------


class _CopyingFs(HostLaneCtxFs):
    """The path this node replaces: each lane keeps its own copy of the roll."""

    def replace_with_link(self, shared: str, roll: str) -> None:
        shutil.copyfile(shared, roll + ".copy")
        os.replace(roll + ".copy", roll)

    def link_new(self, source: str, target: str) -> None:
        shutil.copyfile(source, target)


def test_ctx_ledger_dedupe_control_copy_path_restored_is_red(tmp_path: Path) -> None:
    runs, store = tmp_path / "runs", tmp_path / "store"
    runs.mkdir()
    a = _make_ctx(runs, "lane-a", {ROLL: b"abc" * 1000})
    b = _make_ctx(runs, "lane-b", {ROLL: b"abc" * 1000})
    _handler(_CopyingFs(), RecordingPublisher()).handle(_command(str(runs), str(store)))
    # The same read the golden test makes goes red: the bytes are equal and the inodes are not.
    assert a[0].read_bytes() == b[0].read_bytes()
    with pytest.raises(AssertionError):
        _assert_shared(a[0], b[0])


# --- the CLI ---------------------------------------------------------------------------------


def test_ctx_ledger_dedupe_cli_without_overlay_is_indeterminate(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("ONEX_SKILL_OVERLAY_ROOTS", raising=False)
    assert main([]) == 3
    assert "INDETERMINATE" in capsys.readouterr().err


def test_ctx_ledger_dedupe_cli_reads_the_overlay_and_links(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    runs, store = tmp_path / "runs", tmp_path / "store"
    runs.mkdir()
    a = _make_ctx(runs, "lane-a", {ROLL: b"abc"}, age=10_000_000_000)
    b = _make_ctx(runs, "lane-b", {ROLL: b"abc"}, age=10_000_000_000)
    overlay = tmp_path / "overlays" / "node_lane_ctx_ledger_dedupe_effect"
    overlay.mkdir(parents=True)
    (overlay / "overlay.yaml").write_text(
        f'runs_root: "{runs}"\nstore_dir: "{store}"\nsettle_seconds: "0"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path / "overlays"))
    monkeypatch.delenv("KAFKA_BROKERS", raising=False)

    assert main(["--check"]) == 0
    assert _inode(a[0]) != _inode(b[0])
    assert main([]) == 0

    _assert_shared(a[0], b[0], store / ROLL)
    out = capsys.readouterr()
    assert "LEDGER-DEDUPE mode=execute" in out.out
    assert "linked=1" in out.out
    assert (
        COMPLETED in out.err
    )  # the completed event, topic-addressed, when there is no bus
