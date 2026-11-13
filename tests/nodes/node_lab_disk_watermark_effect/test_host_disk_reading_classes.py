# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC1 (OMN-23865): one typed event per host run, with the volumes and the consumers by class."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

from omnimarket.models.model_host_disk_reading import (
    ALL_CLASSES,
    ModelHostDiskRead,
)
from omnimarket.nodes.node_lab_disk_watermark_effect.handlers.host_disk_classes import (
    parse_docker_size,
    volume_for,
)
from omnimarket.nodes.node_lab_disk_watermark_effect.handlers.host_disk_ports import (
    InMemoryHostDiskPublisher,
)
from omnimarket.nodes.node_lab_disk_watermark_effect.models.model_lab_disk_watermark import (
    ModelLabDiskWatermarkRequest,
)

from .host_disk_inputs import (
    GIB,
    READ_TOPIC,
    handler,
    make_host,
    overlay_document,
    write_overlay,
)

pytestmark = pytest.mark.unit


def _read(
    tmp_path: Path, held: Sequence[Path] = ()
) -> tuple[ModelHostDiskRead, InMemoryHostDiskPublisher]:
    paths = make_host(tmp_path)
    overlay = write_overlay(tmp_path, overlay_document(paths))
    publisher = InMemoryHostDiskPublisher()
    result = handler(tmp_path, publisher, held=held).handle(
        ModelLabDiskWatermarkRequest(host_name="pool-a", overlay_file=str(overlay))
    )
    assert isinstance(result.host_disk, ModelHostDiskRead)
    return result.host_disk, publisher


def test_one_event_per_run_carries_the_volumes_and_every_class(tmp_path: Path) -> None:
    event, publisher = _read(tmp_path)
    assert [topic for topic, _ in publisher.published] == [READ_TOPIC]
    assert publisher.published[0][1] == event
    assert event.correlation_id == "corr-1"
    assert event.host_name == "pool-a"
    (volume,) = event.volumes
    assert (volume.total_bytes, volume.used_bytes, volume.free_bytes) == (
        100 * GIB,
        50 * GIB,
        50 * GIB,
    )
    assert [c.class_name for c in event.classes] == list(ALL_CLASSES)
    assert all(c.status == "read" for c in event.classes)


def test_the_lane_package_cache_is_grouped_by_package_and_revision(
    tmp_path: Path,
) -> None:
    event, _ = _read(tmp_path)
    cache = next(c for c in event.classes if c.class_name == "uv_cache")
    assert cache.entry_count == 5
    assert cache.revision_count == 3
    groups = {(g.package, g.revision): g for g in cache.groups}
    old = groups[("omnimarket", "1.0")]
    assert old.copies == 3
    assert (old.newest_use_age_seconds, old.oldest_use_age_seconds) == (1800, 7200)
    assert groups[("omnimarket", "2.0")].copies == 1
    assert groups[("requests", "2.31")].oldest_use_age_seconds == 90000
    assert cache.total_bytes == sum(g.bytes for g in cache.groups) > 4 * 4096


def test_a_live_holder_marks_its_entry_held(tmp_path: Path) -> None:
    paths = make_host(tmp_path)
    held = (paths["uv"] / "archive-v0" / "h2",)
    overlay = write_overlay(tmp_path, overlay_document(paths))
    result = handler(tmp_path, InMemoryHostDiskPublisher(), held=held).handle(
        ModelLabDiskWatermarkRequest(host_name="pool-a", overlay_file=str(overlay))
    )
    assert isinstance(result.host_disk, ModelHostDiskRead)
    cache = next(c for c in result.host_disk.classes if c.class_name == "uv_cache")
    groups = {(g.package, g.revision): g for g in cache.groups}
    assert groups[("omnimarket", "2.0")].held_copies == 1
    assert groups[("omnimarket", "1.0")].held_copies == 0


def test_worktrees_lab_run_and_docker_classes_are_read(tmp_path: Path) -> None:
    event, _ = _read(tmp_path)
    by_name = {c.class_name: c for c in event.classes}
    assert [g.package for g in by_name["worktrees"].groups] == ["OMN-1"]
    assert [g.package for g in by_name["lab_run"].groups] == ["run-a"]
    assert by_name["docker_images"].total_bytes == 2_500_000_000
    assert by_name["docker_volumes"].total_bytes == 1_000_000_000
    assert by_name["docker_build_cache"].total_bytes == 300_000_000
    exited = by_name["docker_exited_containers"]
    assert (exited.entry_count, exited.total_bytes) == (2, 30_000_000)
    assert all(g.newest_use_age_seconds is None for g in exited.groups)


def test_the_reading_file_keeps_the_shape_placement_reads(tmp_path: Path) -> None:
    paths = make_host(tmp_path)
    overlay = write_overlay(tmp_path, overlay_document(paths))
    handler(tmp_path, InMemoryHostDiskPublisher()).handle(
        ModelLabDiskWatermarkRequest(host_name="pool-a", overlay_file=str(overlay))
    )
    written = (tmp_path / "state" / "pool-a.json").read_text()
    assert '"level": "ok"' in written
    assert "host_disk" not in written


def test_a_dry_run_measures_and_publishes_nothing(tmp_path: Path) -> None:
    paths = make_host(tmp_path)
    overlay = write_overlay(tmp_path, overlay_document(paths))
    publisher = InMemoryHostDiskPublisher()
    result = handler(tmp_path, publisher).handle(
        ModelLabDiskWatermarkRequest(
            host_name="pool-a", overlay_file=str(overlay), dry_run=True
        )
    )
    assert isinstance(result.host_disk, ModelHostDiskRead)
    assert publisher.published == []
    assert not (tmp_path / "state").exists()


def test_without_a_reading_block_the_run_publishes_nothing(tmp_path: Path) -> None:
    paths = make_host(tmp_path)
    overlay = write_overlay(tmp_path, overlay_document(paths, reading=False))
    publisher = InMemoryHostDiskPublisher()
    result = handler(tmp_path, publisher).handle(
        ModelLabDiskWatermarkRequest(host_name="pool-a", overlay_file=str(overlay))
    )
    assert result.host_disk is None
    assert publisher.published == []


def test_volume_for_picks_the_longest_volume_prefix() -> None:
    assert volume_for("/data/x/y", ["/", "/data", "/data/x"]) == "/data/x"
    assert volume_for("/database", ["/", "/data"]) == "/"
    assert volume_for("/mnt/a", ["/data"]) is None


@pytest.mark.parametrize(
    ("text", "size"),
    [
        ("0B", 0),
        ("12.3kB", 12_300),
        ("1.2MB", 1_200_000),
        ("4.5GB", 4_500_000_000),
        ("2TB", 2 * 10**12),
        ("0B (virtual 1.2GB)", 0),
    ],
)
def test_docker_sizes_are_one_thousand_based(text: str, size: int) -> None:
    assert parse_docker_size(text) == size


def test_a_text_that_is_not_a_size_is_refused() -> None:
    with pytest.raises(ValueError, match="not a docker size"):
        parse_docker_size("many")


def test_one_walk_counts_a_hardlinked_file_once_and_reports_the_newest_change(
    tmp_path: Path,
) -> None:
    import os

    from omnimarket.nodes.node_lab_disk_watermark_effect.handlers.host_disk_classes import (
        walk_entry,
    )

    entry = tmp_path / "entry"
    (entry / "a").mkdir(parents=True)
    (entry / "a" / "one").write_bytes(b"x" * 8192)
    os.link(entry / "a" / "one", entry / "two")
    os.utime(entry / "a" / "one", (1000, 1000))
    os.utime(entry / "a", (2000, 2000))
    os.utime(entry, (500, 500))
    walked = walk_entry(entry)
    single = os.lstat(entry / "two").st_blocks * 512
    dirs = os.lstat(entry).st_blocks * 512 + os.lstat(entry / "a").st_blocks * 512
    assert walked.size == single + dirs
    assert walked.newest == 2000
