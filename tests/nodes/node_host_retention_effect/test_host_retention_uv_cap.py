# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The uv cache size cap: evict whole entries oldest-first until the measured total is at or under cap_gib.

The entries are the unpacked wheels of ``archive-v0`` (depth 1) and the git dependency checkouts of ``git-v0/checkouts``
(depth 2: repository, then revision), ordered oldest first across the buckets the overlay lists. An entry is kept when it is
linked into a virtual environment, changed within keep_days, named by a live process, or a symlink or not a folder. A dry run
lists the entries it would evict and removes nothing. A scan or references failure adds an error and evicts nothing (the prune
result stands). The overlay's cap buckets must be named by the contract's bounds.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_host_retention_effect.handlers.handler_host_retention import (
    CONTRACT_PATH,
    HandlerHostRetention,
    load_bounds,
)
from omnimarket.nodes.node_host_retention_effect.handlers.host_retention_ports import (
    FilesystemUvArchive,
    HostRetentionPorts,
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
    FakeUv,
    FakeUvArchive,
    age,
    entry,
    make_ports,
    overlay_file,
)

pytestmark = pytest.mark.unit

_GIB = 2**30


def _uv_cache(
    path: Path,
    *,
    min_gib: float = 20.0,
    cap_gib: float | None = None,
    buckets: tuple[str, ...] = ("archive-v0",),
    keep_days: float = 7.0,
) -> dict[str, object]:
    cache: dict[str, object] = {
        "path": str(path),
        "uv_binary": "uv",
        "min_gib": min_gib,
        "ci_prune": False,
        "timeout_seconds": 1800,
    }
    if cap_gib is not None:
        cache["cap"] = {
            "cap_gib": cap_gib,
            "buckets": list(buckets),
            "keep_days": keep_days,
        }
    return cache


def _entries(*specs: tuple[str, int, float, bool, bool]) -> list[ModelUvArchiveEntry]:
    """specs: (name, bytes, newest_mtime, linked, special)."""
    return [
        ModelUvArchiveEntry(
            name=name, bytes=bytes_, newest_mtime=newest, linked=linked, special=special
        )
        for name, bytes_, newest, linked, special in specs
    ]


def _run_uv(
    tmp_path: Path, cache: dict[str, object], *, ports, refs=None, dry_run=False
):
    handler = HandlerHostRetention(
        now=lambda: NOW,
        references=lambda: ProcessReferences() if refs is None else refs,
        ports=ports,
    )
    return handler.handle(
        ModelHostRetentionRequest(
            host_name="pool-a",
            overlay_file=overlay_file(tmp_path, uv_caches=[cache]),
            dry_run=dry_run,
        )
    )


def test_golden_cap_evicts_oldest_unlinked_entries_until_under_cap(
    tmp_path: Path,
) -> None:
    """Cache above cap: the oldest unlinked entry goes, the pass stops at the cap, the rest stay."""
    path = tmp_path / "uv-cache"
    path.mkdir()
    entries = _entries(
        ("old-a", 10 * _GIB, NOW - 30 * 86400.0, False, False),
        ("old-b", 10 * _GIB, NOW - 20 * 86400.0, False, False),
        ("mid", 10 * _GIB, NOW - 10 * 86400.0, False, False),
        ("new", 10 * _GIB, NOW - 1 * 86400.0, False, False),
    )
    archive = FakeUvArchive(entries=entries)
    uv = FakeUv(size=50 * _GIB, after=40 * _GIB)
    ports = make_ports(uv=uv, uv_archive=archive)
    cache = _uv_cache(path, cap_gib=30.0)

    result = _run_uv(tmp_path, cache, ports=ports)

    assert result.status == "ok"
    # The prune leaves 40 GiB; evicting old-a alone reaches the 30 GiB cap, so the pass stops there.
    evicted = [Path(i.path).name for i in result.items if i.action == "evict"]
    assert evicted == ["old-a"]
    assert archive.removed == ["archive-v0/old-a"]
    # The pass stops at the cap: the entries it did not need to consider are not listed at all.
    assert result.kept == ()
    totals = {t.class_name: t for t in result.totals}
    assert totals["uv_cache"].removed_count == 2  # the prune's done() plus old-a's
    assert (
        totals["uv_cache"].freed_bytes == 20 * _GIB
    )  # the prune freed 10 GiB, old-a 10 GiB


def test_without_cap_nothing_is_evicted(tmp_path: Path) -> None:
    """Failing control: the same fixture with cap absent evicts nothing."""
    path = tmp_path / "uv-cache"
    path.mkdir()
    entries = _entries(
        ("old-a", 10 * _GIB, NOW - 30 * 86400.0, False, False),
        ("old-b", 10 * _GIB, NOW - 20 * 86400.0, False, False),
    )
    archive = FakeUvArchive(entries=entries)
    uv = FakeUv(size=50 * _GIB, after=40 * _GIB)
    ports = make_ports(uv=uv, uv_archive=archive)
    cache = _uv_cache(path)

    result = _run_uv(tmp_path, cache, ports=ports)

    assert result.status == "ok"
    assert not any(i.action == "evict" for i in result.items)
    assert archive.removed == []


def test_linked_entry_survives_even_when_oldest(tmp_path: Path) -> None:
    path = tmp_path / "uv-cache"
    path.mkdir()
    entries = _entries(
        ("linked", 10 * _GIB, NOW - 40 * 86400.0, True, False),
        ("plain", 10 * _GIB, NOW - 30 * 86400.0, False, False),
    )
    archive = FakeUvArchive(entries=entries)
    uv = FakeUv(size=50 * _GIB, after=40 * _GIB)
    ports = make_ports(uv=uv, uv_archive=archive)
    cache = _uv_cache(path, cap_gib=30.0)

    result = _run_uv(tmp_path, cache, ports=ports)

    kept = {Path(k.path).name: k.reason for k in result.kept}
    assert kept["linked"] == "linked_by_venv"
    evicted = [Path(i.path).name for i in result.items if i.action == "evict"]
    assert evicted == ["plain"]


def test_entry_modified_inside_keep_days_is_kept(tmp_path: Path) -> None:
    path = tmp_path / "uv-cache"
    path.mkdir()
    entries = _entries(
        ("fresh", 10 * _GIB, NOW - 1 * 86400.0, False, False),
        ("stale", 10 * _GIB, NOW - 30 * 86400.0, False, False),
    )
    archive = FakeUvArchive(entries=entries)
    uv = FakeUv(size=50 * _GIB, after=40 * _GIB)
    ports = make_ports(uv=uv, uv_archive=archive)
    # cap_gib 25 keeps the pass working after the stale entry goes (30 GiB still over 25 GiB),
    # so the fresh entry is examined and held for its age rather than left for the cap.
    cache = _uv_cache(path, cap_gib=25.0, keep_days=7.0)

    result = _run_uv(tmp_path, cache, ports=ports)

    kept = {Path(k.path).name: k.reason for k in result.kept}
    assert kept["fresh"] == "too_new"
    evicted = [Path(i.path).name for i in result.items if i.action == "evict"]
    assert evicted == ["stale"]


def test_entry_named_by_a_live_process_is_kept(tmp_path: Path) -> None:
    path = tmp_path / "uv-cache"
    path.mkdir()
    entries = _entries(
        ("held", 10 * _GIB, NOW - 30 * 86400.0, False, False),
        ("plain", 10 * _GIB, NOW - 20 * 86400.0, False, False),
    )
    archive = FakeUvArchive(entries=entries)
    uv = FakeUv(size=50 * _GIB, after=40 * _GIB)
    ports = make_ports(uv=uv, uv_archive=archive)
    cache = _uv_cache(path, cap_gib=30.0)
    refs = ProcessReferences(blobs=[f"{path}/archive-v0/held/bin/python run"])

    result = _run_uv(tmp_path, cache, ports=ports, refs=refs)

    kept = {Path(k.path).name: k.reason for k in result.kept}
    assert kept["held"] == "held_by_process"
    evicted = [Path(i.path).name for i in result.items if i.action == "evict"]
    assert evicted == ["plain"]


def test_special_file_is_kept(tmp_path: Path) -> None:
    path = tmp_path / "uv-cache"
    path.mkdir()
    entries = _entries(
        ("special", 10 * _GIB, NOW - 30 * 86400.0, False, True),
        ("plain", 10 * _GIB, NOW - 20 * 86400.0, False, False),
    )
    archive = FakeUvArchive(entries=entries)
    uv = FakeUv(size=50 * _GIB, after=40 * _GIB)
    ports = make_ports(uv=uv, uv_archive=archive)
    cache = _uv_cache(path, cap_gib=30.0)

    result = _run_uv(tmp_path, cache, ports=ports)

    kept = {Path(k.path).name: k.reason for k in result.kept}
    assert kept["special"] == "special_file"
    evicted = [Path(i.path).name for i in result.items if i.action == "evict"]
    assert evicted == ["plain"]


def test_dry_run_lists_entries_and_removes_nothing(tmp_path: Path) -> None:
    path = tmp_path / "uv-cache"
    path.mkdir()
    entries = _entries(
        ("old-a", 10 * _GIB, NOW - 30 * 86400.0, False, False),
        ("old-b", 10 * _GIB, NOW - 20 * 86400.0, False, False),
    )
    archive = FakeUvArchive(entries=entries)
    uv = FakeUv(size=50 * _GIB, after=40 * _GIB)
    ports = make_ports(uv=uv, uv_archive=archive)
    cache = _uv_cache(path, cap_gib=30.0)

    result = _run_uv(tmp_path, cache, ports=ports, dry_run=True)

    evicted = [Path(i.path).name for i in result.items if i.action == "evict"]
    assert evicted == ["old-a", "old-b"]
    assert archive.removed == []
    assert all(
        "dry run" in (i.reason or "") for i in result.items if i.action == "evict"
    )


def test_cap_gib_below_min_gib_is_refused_at_overlay_load(tmp_path: Path) -> None:
    path = tmp_path / "uv-cache"
    path.mkdir()
    cache = _uv_cache(path, min_gib=20.0, cap_gib=10.0)
    result = _run_uv(tmp_path, cache, ports=make_ports())
    assert result.status == "refused"
    assert any("uv_caches" in r for r in result.refusals), result.refusals
    assert result.items == ()
    # Control: a cap equal to min_gib is valid, so the refusal above is the cap's doing.
    ok = _run_uv(
        tmp_path, _uv_cache(path, min_gib=20.0, cap_gib=20.0), ports=make_ports()
    )
    assert ok.status == "ok"


def test_a_cap_bucket_not_named_by_contract_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "uv-cache"
    path.mkdir()
    cache = _uv_cache(path, cap_gib=30.0, buckets=("archive-v0", "not-named"))
    result = _run_uv(tmp_path, cache, ports=make_ports())
    assert result.status == "refused"
    assert any("'not-named'" in r and "contract" in r for r in result.refusals)
    assert result.items == ()


def test_keep_days_below_contract_floor_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "uv-cache"
    path.mkdir()
    cache = _uv_cache(path, cap_gib=30.0, keep_days=0.5)
    result = _run_uv(tmp_path, cache, ports=make_ports())
    assert result.status == "refused"
    assert any("keep_days" in r for r in result.refusals)


def test_scan_failure_adds_error_and_evicts_nothing(tmp_path: Path) -> None:
    path = tmp_path / "uv-cache"
    path.mkdir()
    archive = FakeUvArchive(fail_scan=True)
    uv = FakeUv(size=50 * _GIB, after=40 * _GIB)
    ports = make_ports(uv=uv, uv_archive=archive)
    cache = _uv_cache(path, cap_gib=30.0)

    result = _run_uv(tmp_path, cache, ports=ports)

    assert result.status == "ok"
    assert any("OSError" in e for e in result.errors)
    assert archive.removed == []
    assert uv.prunes, "the prune still ran"


def test_a_failed_prune_is_an_error_and_the_cap_still_evicts_from_the_size_read_before_it(
    tmp_path: Path,
) -> None:
    """Found on h101: the prune failed (the cache lock was held), and the cap pass must not be skipped with it."""
    path = tmp_path / "uv-cache"
    path.mkdir()
    archive = FakeUvArchive(
        entries=_entries(("old", 10 * _GIB, NOW - 30 * 86400.0, False, False))
    )
    uv = FakeUv(size=40 * _GIB, after=40 * _GIB, fail=True)
    cache = _uv_cache(path, cap_gib=35.0)

    result = _run_uv(tmp_path, cache, ports=make_ports(uv=uv, uv_archive=archive))

    assert len(result.errors) == 1
    assert "OSError" in result.errors[0]
    assert archive.removed == ["archive-v0/old"]


def test_unreadable_process_references_add_error_and_evict_nothing(
    tmp_path: Path,
) -> None:
    path = tmp_path / "uv-cache"
    path.mkdir()
    entries = _entries(("old-a", 10 * _GIB, NOW - 30 * 86400.0, False, False))
    archive = FakeUvArchive(entries=entries)
    uv = FakeUv(size=50 * _GIB, after=40 * _GIB)
    ports = make_ports(uv=uv, uv_archive=archive)
    cache = _uv_cache(path, cap_gib=30.0)

    handler = HandlerHostRetention(
        now=lambda: NOW,
        references=lambda: None,
        ports=ports,
    )
    result = handler.handle(
        ModelHostRetentionRequest(
            host_name="pool-a",
            overlay_file=overlay_file(tmp_path, uv_caches=[cache]),
        )
    )

    assert result.status == "ok"
    assert any("references" in e for e in result.errors)
    assert archive.removed == []


def test_seam_real_archive_reads_linked_and_unlinked(tmp_path: Path) -> None:
    """The real FilesystemUvArchive over a tmp_path tree with a real hardlink reads linked=True and an unlinked sibling linked=False."""
    bucket = tmp_path / "archive-v0"
    bucket.mkdir()
    linked_dir = bucket / "linked"
    linked_dir.mkdir()
    (linked_dir / "f").write_bytes(b"x" * 4096)
    os.link(str(linked_dir / "f"), str(linked_dir / "hard"))
    plain_dir = bucket / "plain"
    plain_dir.mkdir()
    (plain_dir / "f").write_bytes(b"y" * 8192)

    archive = FilesystemUvArchive()
    entries = {e.name: e for e in archive.scan(str(bucket), 1, 1800.0)}
    assert entries["linked"].linked is True
    assert entries["plain"].linked is False
    assert entries["linked"].bytes == 8192
    assert entries["plain"].bytes == 8192


def test_seam_real_archive_reads_special_for_symlink(tmp_path: Path) -> None:
    bucket = tmp_path / "archive-v0"
    bucket.mkdir()
    target = tmp_path / "target"
    target.mkdir()
    (target / "f").write_bytes(b"x" * 4096)
    os.symlink(str(target), str(bucket / "link"))

    archive = FilesystemUvArchive()
    entries = {e.name: e for e in archive.scan(str(bucket), 1, 1800.0)}
    assert entries["link"].special is True


def test_seam_real_archive_remove_refuses_separator_and_symlink(tmp_path: Path) -> None:
    bucket = tmp_path / "archive-v0"
    bucket.mkdir()
    (bucket / "real").mkdir()
    os.symlink(str(bucket / "real"), str(bucket / "link"))

    archive = FilesystemUvArchive()
    with pytest.raises(OSError, match=r"is not"):
        archive.remove(str(bucket), "a/b")
    with pytest.raises(OSError, match="symlink"):
        archive.remove(str(bucket), "link")
    assert (bucket / "real").exists()


def test_seam_real_archive_reads_git_checkouts_two_levels_deep(tmp_path: Path) -> None:
    """uv's git cache is ``checkouts/<repository>/<revision>``: at depth 2 the entries are the revisions."""
    bucket = tmp_path / "git-v0" / "checkouts"
    (bucket / "repoA" / "rev1").mkdir(parents=True)
    (bucket / "repoA" / "rev1" / "f").write_bytes(b"x" * 4096)
    (bucket / "repoA" / "rev2").mkdir()
    (bucket / "repoB" / "rev9").mkdir(parents=True)
    (bucket / "repoB" / "rev9" / "f").write_bytes(b"y" * 8192)
    (bucket / "stray-file").write_bytes(b"z")

    archive = FilesystemUvArchive()
    entries = {e.name: e for e in archive.scan(str(bucket), 2, 1800.0)}

    assert set(entries) == {"repoA/rev1", "repoA/rev2", "repoB/rev9", "stray-file"}
    assert entries["repoA/rev1"].bytes == 4096
    assert entries["repoB/rev9"].bytes == 8192
    assert entries["stray-file"].special is True
    archive.remove(str(bucket), "repoA/rev1")
    assert not (bucket / "repoA" / "rev1").exists()
    assert (bucket / "repoA" / "rev2").exists(), "a sibling revision stays"
    with pytest.raises(OSError, match=r"is not"):
        archive.remove(str(bucket), "repoA/../repoB/rev9")


def test_seam_real_archive_scan_past_its_deadline_is_an_error_not_a_partial_list(
    tmp_path: Path,
) -> None:
    bucket = tmp_path / "archive-v0"
    (bucket / "a").mkdir(parents=True)
    (bucket / "a" / "f").write_bytes(b"x")

    with pytest.raises(OSError, match="deadline"):
        FilesystemUvArchive().scan(str(bucket), 1, -1.0)


def test_seam_real_uv_layout_names_two_buckets_and_the_overlay_example_loads() -> None:
    """The contract names uv's two growing folders with their depths; the overlay example's cap buckets are all among them."""
    bounds = load_bounds()
    assert {
        name: (bound.depth, bound.keep_linked)
        for name, bound in bounds.uv_evictable_buckets.items()
    } == {"archive-v0": (1, True), "git-v0/checkouts": (2, False)}
    overlay = yaml.safe_load(
        (CONTRACT_PATH.parent / "overlay.example.yaml").read_text(encoding="utf-8")
    )
    cap_buckets = [
        bucket
        for host in overlay["hosts"]
        for cache in host.get("uv_caches", [])
        if "cap" in cache
        for bucket in cache["cap"]["buckets"]
    ]
    assert cap_buckets, "the overlay example declares a cap"
    assert set(cap_buckets) <= set(bounds.uv_evictable_buckets)


def test_oldest_first_runs_across_both_buckets_and_scans_each_at_its_depth(
    tmp_path: Path,
) -> None:
    """A revision checkout older than every wheel goes first; each bucket is scanned at the depth the contract declares."""
    path = tmp_path / "uv-cache"
    path.mkdir()
    day = 86400.0
    archive = FakeUvArchive(
        entries={
            "archive-v0": _entries(
                ("wheel-old", 10 * _GIB, NOW - 20 * day, False, False),
                ("wheel-new", 10 * _GIB, NOW - 2 * day, False, False),
            ),
            "git-v0/checkouts": _entries(
                ("repoA/rev-oldest", 10 * _GIB, NOW - 40 * day, False, False),
                ("repoA/rev-mid", 10 * _GIB, NOW - 10 * day, False, False),
            ),
        }
    )
    uv = FakeUv(size=60 * _GIB, after=60 * _GIB)
    cache = _uv_cache(path, cap_gib=30.0, buckets=("archive-v0", "git-v0/checkouts"))

    result = _run_uv(tmp_path, cache, ports=make_ports(uv=uv, uv_archive=archive))

    assert archive.removed == [
        "git-v0/checkouts/repoA/rev-oldest",
        "archive-v0/wheel-old",
        "git-v0/checkouts/repoA/rev-mid",
    ]
    assert sorted(archive.scans) == [
        (str(path / "archive-v0"), 1),
        (str(path / "git-v0" / "checkouts"), 2),
    ]
    assert result.errors == ()


def test_a_revision_a_live_process_names_is_kept_but_its_sibling_goes(
    tmp_path: Path,
) -> None:
    path = tmp_path / "uv-cache"
    path.mkdir()
    archive = FakeUvArchive(
        entries={
            "git-v0/checkouts": _entries(
                ("repoA/held", 10 * _GIB, NOW - 40 * 86400.0, False, False),
                ("repoA/free", 10 * _GIB, NOW - 30 * 86400.0, False, False),
            )
        }
    )
    uv = FakeUv(size=40 * _GIB, after=40 * _GIB)
    cache = _uv_cache(path, cap_gib=30.0, buckets=("git-v0/checkouts",))
    refs = ProcessReferences(
        blobs=[f"{path}/git-v0/checkouts/repoA/held/pyproject.toml build"]
    )

    result = _run_uv(
        tmp_path, cache, ports=make_ports(uv=uv, uv_archive=archive), refs=refs
    )

    assert archive.removed == ["git-v0/checkouts/repoA/free"]
    assert {Path(k.path).name: k.reason for k in result.kept} == {
        "held": "held_by_process"
    }


def test_one_bucket_failing_to_scan_is_an_error_and_the_other_still_evicts(
    tmp_path: Path,
) -> None:
    path = tmp_path / "uv-cache"
    path.mkdir()
    archive = FakeUvArchive(
        entries={
            "archive-v0": _entries(
                ("wheel-old", 10 * _GIB, NOW - 20 * 86400.0, False, False)
            ),
            "git-v0/checkouts": [],
        },
        fail_buckets=("git-v0/checkouts",),
    )
    uv = FakeUv(size=40 * _GIB, after=40 * _GIB)
    cache = _uv_cache(path, cap_gib=35.0, buckets=("archive-v0", "git-v0/checkouts"))

    result = _run_uv(tmp_path, cache, ports=make_ports(uv=uv, uv_archive=archive))

    assert archive.removed == ["archive-v0/wheel-old"]
    assert len(result.errors) == 1
    assert "git-v0/checkouts" in result.errors[0]


def _real_archive_ports(uv: FakeUv) -> HostRetentionPorts:
    """The ports of a real pass over a real folder tree: only the uv binary (sizes, prune) is a fake."""
    base = make_ports(uv=uv)
    return HostRetentionPorts(
        runner_state=base.runner_state,
        runner_remove=base.runner_remove,
        uv_cache=base.uv_cache,
        uv_archive=FilesystemUvArchive(),
        lane_liveness=base.lane_liveness,
        container_mounts=base.container_mounts,
        unit_files=base.unit_files,
        disk=base.disk,
        pre_commit_store=base.pre_commit_store,
    )


def _uv_tree(cache: Path) -> None:
    """A cache laid out as uv writes it: unpacked wheels and per-revision git checkouts."""
    archive = cache / "archive-v0"
    entry(archive, "wheel-old", days=40)
    entry(archive, "wheel-venv", days=40)
    os.link(archive / "wheel-venv" / "f0", archive / "wheel-venv" / "venv-link")
    age(archive / "wheel-venv", 40)
    entry(archive, "wheel-new", days=1)
    checkouts = cache / "git-v0" / "checkouts"
    entry(checkouts / "repoA", "rev-old", days=30)
    entry(checkouts / "repoA", "rev-new", days=2)
    # `git clone --local` hard-links a checkout's objects to the git database: the link ties it to uv's database, not to a venv.
    db = cache / "git-v0" / "db" / "repoA"
    db.mkdir(parents=True)
    (db / "pack").write_bytes(b"x" * 4096)
    os.link(db / "pack", checkouts / "repoA" / "rev-old" / "pack")
    age(checkouts / "repoA" / "rev-old", 30)


def test_golden_chain_the_cap_pass_removes_aged_unlinked_entries_from_both_buckets_on_disk(
    tmp_path: Path,
) -> None:
    """A real tree through the real archive port: aged unlinked wheels and revisions go; a venv-linked wheel and recent ones stay."""
    cache = tmp_path / "uv-cache"
    _uv_tree(cache)
    uv = FakeUv(size=50 * _GIB, after=50 * _GIB)
    # Every entry reads 8 KiB, so a 1 GiB cap is never reached by the entries the pass may take.
    spec = _uv_cache(
        cache, min_gib=1.0, cap_gib=1.0, buckets=("archive-v0", "git-v0/checkouts")
    )

    result = _run_uv(tmp_path, spec, ports=_real_archive_ports(uv))

    assert result.status == "ok"
    assert result.errors == ()
    assert not (cache / "archive-v0" / "wheel-old").exists()
    assert not (cache / "git-v0" / "checkouts" / "repoA" / "rev-old").exists()
    assert (cache / "archive-v0" / "wheel-venv" / "venv-link").exists()
    assert (cache / "archive-v0" / "wheel-new").exists()
    assert (cache / "git-v0" / "checkouts" / "repoA" / "rev-new").exists()
    kept = {Path(k.path).name: k.reason for k in result.kept}
    assert kept == {
        "wheel-venv": "linked_by_venv",
        "wheel-new": "too_new",
        "rev-new": "too_new",
    }
    assert {Path(i.path).name for i in result.items if i.action == "evict"} == {
        "wheel-old",
        "rev-old",
    }


def test_a_git_checkout_hard_linked_to_the_git_database_is_evicted_but_a_venv_linked_wheel_is_kept(
    tmp_path: Path,
) -> None:
    """Failing control for the per-folder guard: the same hard link keeps an archive entry and does not keep a checkout."""
    path = tmp_path / "uv-cache"
    path.mkdir()
    day = 86400.0
    archive = FakeUvArchive(
        entries={
            "archive-v0": _entries(("wheel", 10 * _GIB, NOW - 40 * day, True, False)),
            "git-v0/checkouts": _entries(
                ("repoA/rev", 10 * _GIB, NOW - 30 * day, True, False)
            ),
        }
    )
    uv = FakeUv(size=40 * _GIB, after=40 * _GIB)
    cache = _uv_cache(path, cap_gib=25.0, buckets=("archive-v0", "git-v0/checkouts"))

    result = _run_uv(tmp_path, cache, ports=make_ports(uv=uv, uv_archive=archive))

    assert archive.removed == ["git-v0/checkouts/repoA/rev"]
    assert {Path(k.path).name: k.reason for k in result.kept} == {
        "wheel": "linked_by_venv"
    }


def test_error_chain_a_cache_without_its_declared_bucket_reports_the_bucket_and_removes_nothing_else(
    tmp_path: Path,
) -> None:
    cache = tmp_path / "uv-cache"
    _uv_tree(cache)
    uv = FakeUv(size=50 * _GIB, after=50 * _GIB)
    spec = _uv_cache(
        cache, min_gib=1.0, cap_gib=1.0, buckets=("archive-v0", "git-v0/checkouts")
    )
    (cache / "git-v0" / "checkouts").rename(cache / "git-v0" / "gone")

    result = _run_uv(tmp_path, spec, ports=_real_archive_ports(uv))

    assert len(result.errors) == 1
    assert "git-v0/checkouts" in result.errors[0]
    assert not (cache / "archive-v0" / "wheel-old").exists(), (
        "the bucket that exists is still evicted"
    )
    assert (cache / "git-v0" / "gone" / "repoA" / "rev-old").exists()
