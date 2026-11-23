# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A cache entry that is listed and then vanishes during the class scan is skipped and counted, never raised (OMN-26097).

The unit on h201 died with exit 3 (``FileNotFoundError`` on a uv-cache ``archive-v0`` entry, and once on a worktree):
the cache is rewritten by other lanes while the scan walks it, and the owner read of a vanished entry raised.
"""

from __future__ import annotations

import itertools
import json
import shutil
from collections.abc import Sequence
from pathlib import Path

import pytest

from omnimarket.models.model_host_disk_reading import ModelHostDiskRead
from omnimarket.nodes.node_lab_disk_watermark_effect.handlers import host_disk_classes
from omnimarket.nodes.node_lab_disk_watermark_effect.handlers.handler_lab_disk_watermark import (
    HandlerLabDiskWatermark,
    Usage,
    load_schedule,
)
from omnimarket.nodes.node_lab_disk_watermark_effect.handlers.host_disk_classes import (
    HostDiskClassReader,
)
from omnimarket.nodes.node_lab_disk_watermark_effect.handlers.host_disk_ports import (
    InMemoryHostDiskPublisher,
)
from omnimarket.nodes.node_lab_disk_watermark_effect.models.model_lab_disk_watermark import (
    ModelLabDiskWatermarkGrowthReading,
    ModelLabDiskWatermarkRequest,
)
from omnimarket.nodes.node_lab_disk_watermark_effect.runtime_lab_disk_watermark import (
    main,
)
from omnimarket.nodes.node_temp_retention_effect.handlers.handler_temp_retention import (
    ProcessReferences,
)

from .host_disk_inputs import (
    GIB,
    NOW,
    fake_docker,
    handler,
    make_host,
    overlay_document,
    write_overlay,
)

pytestmark = pytest.mark.unit


def _vanish_after_listing(
    monkeypatch: pytest.MonkeyPatch, victims: Sequence[str]
) -> None:
    """Remove each named entry the moment the scan first walks it: listed by ``iterdir``, gone before it is read."""
    real_walk = host_disk_classes.walk_entry

    def walk(path: Path) -> host_disk_classes._Walk:
        if path.name in victims and path.exists():
            shutil.rmtree(path)
        return real_walk(path)

    monkeypatch.setattr(host_disk_classes, "walk_entry", walk)


def _handle(
    tmp_path: Path,
) -> tuple[ModelLabDiskWatermarkGrowthReading, InMemoryHostDiskPublisher]:
    paths = make_host(tmp_path)
    overlay = write_overlay(tmp_path, overlay_document(paths))
    publisher = InMemoryHostDiskPublisher()
    result = handler(tmp_path, publisher).handle(
        ModelLabDiskWatermarkRequest(host_name="pool-a", overlay_file=str(overlay))
    )
    return result, publisher


def test_a_uv_cache_entry_that_vanishes_mid_scan_is_skipped_and_counted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _vanish_after_listing(monkeypatch, ["h10"])
    result, publisher = _handle(tmp_path)
    assert isinstance(result.host_disk, ModelHostDiskRead)
    cache = next(c for c in result.host_disk.classes if c.class_name == "uv_cache")
    assert cache.status == "read"
    assert cache.entry_count == 4
    assert [(s.class_name, s.skipped_entries) for s in result.scan_skipped] == [
        ("uv_cache", 1)
    ]
    assert len(publisher.published) == 1


def test_a_worktree_that_vanishes_mid_scan_is_skipped_and_counted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _vanish_after_listing(monkeypatch, ["OMN-1"])
    result, _ = _handle(tmp_path)
    assert isinstance(result.host_disk, ModelHostDiskRead)
    worktrees = next(c for c in result.host_disk.classes if c.class_name == "worktrees")
    assert worktrees.status == "read"
    assert worktrees.entry_count == 0
    assert [(s.class_name, s.skipped_entries) for s in result.scan_skipped] == [
        ("worktrees", 1)
    ]


def test_a_scan_with_nothing_vanishing_counts_no_skips(tmp_path: Path) -> None:
    result, _ = _handle(tmp_path)
    assert result.scan_skipped == ()


def test_a_missing_cache_root_is_an_unread_class_never_a_raise(tmp_path: Path) -> None:
    paths = make_host(tmp_path)
    shutil.rmtree(paths["uv"])
    overlay = write_overlay(tmp_path, overlay_document(paths))
    result = handler(tmp_path, InMemoryHostDiskPublisher()).handle(
        ModelLabDiskWatermarkRequest(host_name="pool-a", overlay_file=str(overlay))
    )
    assert isinstance(result.host_disk, ModelHostDiskRead)
    cache = next(c for c in result.host_disk.classes if c.class_name == "uv_cache")
    assert cache.status == "unread"
    assert "unreadable" in cache.unread_reason


def test_the_unit_exits_zero_when_an_entry_vanishes_mid_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The failure as the unit saw it: ``main`` turned the raise into ``INDETERMINATE`` and exit 3."""
    paths = make_host(tmp_path)
    overlay = write_overlay(tmp_path, overlay_document(paths, docker=False))
    _vanish_after_listing(monkeypatch, ["h10"])
    monkeypatch.setenv("HOME", str(tmp_path))
    code = main(["run", "--host", "pool-a", "--overlay", str(overlay), "--dry-run"])
    captured = capsys.readouterr()
    assert "INDETERMINATE" not in captured.err
    assert code == 0


# --- the 600 s start timeout: the class scan has a budget and keeps what it walked (OMN-26097) ---------------------------


@pytest.fixture
def walks(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The names of the entries the scan walks, in order."""
    seen: list[str] = []
    real_walk = host_disk_classes.walk_entry

    def counting_walk(path: Path) -> host_disk_classes._Walk:
        seen.append(path.name)
        return real_walk(path)

    monkeypatch.setattr(host_disk_classes, "walk_entry", counting_walk)
    return seen


def _budgeted_handler(tmp_path: Path, *, tick: float = 1.0) -> HandlerLabDiskWatermark:
    """A handler whose scan clock moves ``tick`` seconds per reading of it."""
    clock = itertools.count()
    refs = ProcessReferences(directories=[], blobs=[], commands=[])
    return HandlerLabDiskWatermark(
        usage=lambda _path: Usage(100 * GIB, 50 * GIB, 50 * GIB),
        now=lambda: NOW,
        home=tmp_path,
        classes=HostDiskClassReader(
            now=NOW.timestamp,
            process_references=lambda: refs,
            run_docker=fake_docker,
            monotonic=lambda: next(clock) * tick,
        ),
        publisher=InMemoryHostDiskPublisher(),
        new_id=lambda: "corr-1",
    )


def _request(
    overlay: Path, budget: float | None, *, dry_run: bool = False
) -> ModelLabDiskWatermarkRequest:
    return ModelLabDiskWatermarkRequest(
        host_name="pool-a",
        overlay_file=str(overlay),
        budget_seconds=budget,
        dry_run=dry_run,
    )


def test_a_scan_that_spends_its_budget_stops_reads_unread_and_the_next_run_finishes(
    tmp_path: Path, walks: list[str]
) -> None:
    paths = make_host(tmp_path)
    overlay = write_overlay(tmp_path, overlay_document(paths))
    first = _budgeted_handler(tmp_path).handle(_request(overlay, 3.5))
    assert isinstance(first.host_disk, ModelHostDiskRead)
    cache = next(c for c in first.host_disk.classes if c.class_name == "uv_cache")
    assert cache.status == "unread"
    assert cache.unread_reason == (
        "scan budget spent after 3 of 5 entries; the walks are kept for the next run"
    )
    assert len(walks) == 3
    kept = json.loads((tmp_path / "state" / "pool-a.json.uv-entries.json").read_text())
    assert len(kept) == 3

    walks.clear()
    second = _budgeted_handler(tmp_path).handle(_request(overlay, 1000.0))
    assert isinstance(second.host_disk, ModelHostDiskRead)
    cache = next(c for c in second.host_disk.classes if c.class_name == "uv_cache")
    assert cache.status == "read"
    assert cache.entry_count == 5
    uv_walks = [name for name in walks if name.startswith("h")]
    assert len(uv_walks) == 2, (
        "only the two entries the first run did not reach are walked"
    )


def test_an_unchanged_uv_entry_is_not_walked_again_and_a_changed_one_is(
    tmp_path: Path, walks: list[str]
) -> None:
    paths = make_host(tmp_path)
    overlay = write_overlay(tmp_path, overlay_document(paths))
    _budgeted_handler(tmp_path).handle(_request(overlay, 1000.0))
    assert sorted(n for n in walks if n.startswith("h")) == [
        "h10",
        "h11",
        "h12",
        "h2",
        "h3",
    ]

    (paths["uv"] / "archive-v0" / "h2" / "new-file").write_bytes(b"n")
    walks.clear()
    again = _budgeted_handler(tmp_path).handle(_request(overlay, 1000.0))
    assert [n for n in walks if n.startswith("h")] == ["h2"]
    assert isinstance(again.host_disk, ModelHostDiskRead)
    cache = next(c for c in again.host_disk.classes if c.class_name == "uv_cache")
    assert cache.entry_count == 5


def test_a_dry_run_reads_the_kept_walks_and_writes_none(
    tmp_path: Path, walks: list[str]
) -> None:
    paths = make_host(tmp_path)
    overlay = write_overlay(tmp_path, overlay_document(paths))
    _budgeted_handler(tmp_path).handle(_request(overlay, 1000.0, dry_run=True))
    assert not (tmp_path / "state" / "pool-a.json.uv-entries.json").exists()


def test_a_malformed_kept_walks_file_is_an_empty_one(
    tmp_path: Path, walks: list[str]
) -> None:
    paths = make_host(tmp_path)
    overlay = write_overlay(tmp_path, overlay_document(paths))
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "pool-a.json.uv-entries.json").write_text("{not json")
    result = _budgeted_handler(tmp_path).handle(_request(overlay, 1000.0))
    assert isinstance(result.host_disk, ModelHostDiskRead)
    assert len([n for n in walks if n.startswith("h")]) == 5


def test_the_declared_scan_budget_leaves_room_for_the_docker_reads_inside_the_start_timeout() -> (
    None
):
    schedule = load_schedule()
    budget = schedule.scan_budget_percent / 100 * schedule.timeout_start_seconds
    docker_reads = (
        2 * 60
    )  # df_command and exited_command, each bounded by the overlay's timeout_seconds
    assert budget + docker_reads < schedule.timeout_start_seconds
