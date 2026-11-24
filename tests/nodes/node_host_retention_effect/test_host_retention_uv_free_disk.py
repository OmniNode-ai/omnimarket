# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The uv cache free-disk and age rule: no size is measured, nothing is pruned, the cache is walked only when the disk is low.

``free_disk`` reads the free space of the cache's filesystem once. At or above ``min_free_gib`` the cache is left alone and its
buckets are not scanned. Below it, whole entries untouched for ``keep_days`` are evicted oldest first until their own bytes bring
the free space to ``target_free_gib``; an entry linked into a virtual environment, new, named by a live process, or not a folder
is kept. A dry run lists and removes nothing. The rule and the size rule exclude each other in the overlay.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from omnimarket.nodes.node_host_retention_effect.handlers.handler_host_retention import (
    HandlerHostRetention,
)
from omnimarket.nodes.node_host_retention_effect.handlers.host_retention_ports import (
    ShutilDisk,
)
from omnimarket.nodes.node_host_retention_effect.models.model_host_retention import (
    ModelHostRetentionRequest,
    ModelUvArchiveEntry,
)
from omnimarket.nodes.node_temp_retention_effect.handlers.handler_temp_retention import (
    ProcessReferences,
)
from tests.nodes.node_host_retention_effect.host_retention_fakes import (
    NOW,
    UNREADABLE,
    FakeDisk,
    FakeUvArchive,
    make_ports,
    overlay_file,
)

pytestmark = pytest.mark.unit

_GIB = 2**30
_DAY = 86400.0


class _ForbiddenUv:
    """The size-measuring port: any call is a failure, because the free-disk rule never sizes or prunes."""

    def size_bytes(self, uv_binary: str, cache_dir: str, timeout_s: float) -> int:
        raise AssertionError("the free-disk rule measured the cache")

    def prune(self, uv_binary: str, cache_dir: str, ci: bool, timeout_s: float) -> None:
        raise AssertionError("the free-disk rule pruned the cache")


def _entry(
    name: str, gib: int, days: float, *, linked: bool = False, special: bool = False
) -> ModelUvArchiveEntry:
    return ModelUvArchiveEntry(
        name=name,
        bytes=gib * _GIB,
        newest_mtime=NOW - days * _DAY,
        linked=linked,
        special=special,
    )


def _cache(
    path: Path,
    *,
    min_free_gib: float = 50.0,
    target_free_gib: float = 60.0,
    buckets: tuple[str, ...] = ("archive-v0",),
    keep_days: float = 7.0,
    **extra: object,
) -> dict[str, object]:
    return {
        "path": str(path),
        "uv_binary": "uv",
        "timeout_seconds": 1800,
        "free_disk": {
            "min_free_gib": min_free_gib,
            "target_free_gib": target_free_gib,
            "buckets": list(buckets),
            "keep_days": keep_days,
        },
        **extra,
    }


def _run(
    tmp_path: Path,
    cache: dict[str, object],
    *,
    free_gib: float,
    archive: FakeUvArchive,
    refs: ProcessReferences | None = None,
    dry_run: bool = False,
):
    handler = HandlerHostRetention(
        now=lambda: NOW,
        references=lambda: (
            ProcessReferences()
            if refs is None
            else None
            if refs is UNREADABLE
            else refs
        ),
        ports=make_ports(
            uv=_ForbiddenUv(),
            uv_archive=archive,
            disk=FakeDisk(free=int(free_gib * _GIB)),
        ),
    )
    return handler.handle(
        ModelHostRetentionRequest(
            host_name="pool-a",
            overlay_file=overlay_file(tmp_path, uv_caches=[cache]),
            dry_run=dry_run,
        )
    )


def _cache_dir(tmp_path: Path) -> Path:
    path = tmp_path / "uv-cache"
    path.mkdir()
    return path


def test_golden_enough_free_disk_leaves_the_cache_unread(tmp_path: Path) -> None:
    """At or above min_free_gib: kept, no scan, no size, no prune."""
    path = _cache_dir(tmp_path)
    archive = FakeUvArchive(entries=[_entry("old", 30, 90)])

    result = _run(tmp_path, _cache(path), free_gib=50.0, archive=archive)

    assert result.status == "ok"
    assert result.errors == ()
    assert result.items == ()
    assert [(Path(k.path).name, k.reason) for k in result.kept] == [
        ("uv-cache", "free_disk_ok")
    ]
    assert archive.scans == []
    assert archive.removed == []


def test_low_free_disk_evicts_oldest_old_entries_until_the_target(
    tmp_path: Path,
) -> None:
    """Free 30 of 60 wanted: 30 GiB must go; the two oldest 20 GiB entries do it and the third stays unconsidered."""
    path = _cache_dir(tmp_path)
    archive = FakeUvArchive(
        entries=[
            _entry("mid", 20, 20),
            _entry("oldest", 20, 60),
            _entry("old", 20, 40),
        ]
    )

    result = _run(tmp_path, _cache(path), free_gib=30.0, archive=archive)

    assert result.status == "ok"
    assert archive.removed == ["archive-v0/oldest", "archive-v0/old"]
    assert [Path(i.path).name for i in result.items if i.action == "evict"] == [
        "old",
        "oldest",
    ]
    totals = {t.class_name: t for t in result.totals}
    assert totals["uv_cache"].removed_count == 2
    assert totals["uv_cache"].freed_bytes == 40 * _GIB
    assert result.kept == ()


def test_the_age_rule_keeps_new_linked_held_and_special_entries(
    tmp_path: Path,
) -> None:
    path = _cache_dir(tmp_path)
    archive = FakeUvArchive(
        entries=[
            _entry("fresh", 20, 1),
            _entry("linked", 20, 90, linked=True),
            _entry("held", 20, 80),
            _entry("odd", 0, 70, special=True),
            _entry("goes", 20, 60),
        ]
    )
    refs = ProcessReferences(blobs=[f"{path}/archive-v0/held/bin/python run"])

    result = _run(tmp_path, _cache(path), free_gib=30.0, archive=archive, refs=refs)

    assert {Path(k.path).name: k.reason for k in result.kept} == {
        "fresh": "too_new",
        "linked": "linked_by_venv",
        "held": "held_by_process",
        "odd": "special_file",
    }
    assert archive.removed == ["archive-v0/goes"]


def test_dry_run_lists_the_entries_and_removes_nothing(tmp_path: Path) -> None:
    path = _cache_dir(tmp_path)
    archive = FakeUvArchive(entries=[_entry("old", 30, 90)])

    result = _run(tmp_path, _cache(path), free_gib=30.0, archive=archive, dry_run=True)

    assert [Path(i.path).name for i in result.items] == ["old"]
    assert all("dry run" in (i.reason or "") for i in result.items)
    assert archive.removed == []


def test_entries_of_two_buckets_go_oldest_first_across_them(tmp_path: Path) -> None:
    path = _cache_dir(tmp_path)
    archive = FakeUvArchive(
        entries={
            "archive-v0": [_entry("wheel", 10, 30)],
            "git-v0/checkouts": [_entry("repo/rev", 10, 50)],
        }
    )
    cache = _cache(path, buckets=("archive-v0", "git-v0/checkouts"))

    result = _run(tmp_path, cache, free_gib=50.0 - 15, archive=archive)

    assert archive.removed == ["git-v0/checkouts/repo/rev", "archive-v0/wheel"]
    assert result.errors == ()


def test_unreadable_process_references_evict_nothing(tmp_path: Path) -> None:
    path = _cache_dir(tmp_path)
    archive = FakeUvArchive(entries=[_entry("old", 30, 90)])

    result = _run(
        tmp_path, _cache(path), free_gib=30.0, archive=archive, refs=UNREADABLE
    )

    assert archive.removed == []
    assert result.errors


def test_a_scan_failure_adds_an_error_and_evicts_nothing(tmp_path: Path) -> None:
    path = _cache_dir(tmp_path)
    archive = FakeUvArchive(entries=[_entry("old", 30, 90)], fail_scan=True)

    result = _run(tmp_path, _cache(path), free_gib=30.0, archive=archive)

    assert archive.removed == []
    assert any("OSError" in e for e in result.errors)


def test_the_overlay_takes_exactly_one_rule(tmp_path: Path) -> None:
    path = _cache_dir(tmp_path)
    archive = FakeUvArchive()
    for cache in (
        _cache(path, min_gib=20.0),  # both rules
        {k: v for k, v in _cache(path).items() if k != "free_disk"},  # neither
        _cache(
            path, cap={"cap_gib": 30.0, "buckets": ["archive-v0"], "keep_days": 7.0}
        ),  # a cap beside free_disk
        _cache(path, min_free_gib=60.0, target_free_gib=50.0),  # target under min
    ):
        result = _run(tmp_path, cache, free_gib=100.0, archive=archive)
        assert result.status == "refused"
        assert any("uv_caches" in r for r in result.refusals), result.refusals
        assert result.items == ()
    # Control: the same cache with the rule alone loads.
    assert _run(tmp_path, _cache(path), free_gib=100.0, archive=archive).status == "ok"


def test_a_bucket_the_contract_does_not_name_is_refused(tmp_path: Path) -> None:
    path = _cache_dir(tmp_path)
    cache = _cache(path, buckets=("archive-v0", "not-named"))

    result = _run(tmp_path, cache, free_gib=30.0, archive=FakeUvArchive())

    assert result.status == "refused"
    assert any("'not-named'" in r and "free_disk" in r for r in result.refusals)


def test_keep_days_below_the_contract_floor_is_refused(tmp_path: Path) -> None:
    path = _cache_dir(tmp_path)

    result = _run(
        tmp_path, _cache(path, keep_days=0.5), free_gib=30.0, archive=FakeUvArchive()
    )

    assert result.status == "refused"
    assert any("free_disk keep_days" in r for r in result.refusals)


def test_the_real_disk_port_reports_free_bytes(tmp_path: Path) -> None:
    reading = ShutilDisk().reading(str(tmp_path))

    assert reading.free_bytes == pytest.approx(
        shutil.disk_usage(tmp_path).free, abs=64 * 2**20
    )
    assert reading.free_bytes > 0
