# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A host row shaped like a lab pool host with relocated runner folders and a uv 0.13 cache.

The host unit asks for the uv cache class alone. Two things about such a row used to refuse the whole pass: the runner mount roots
are symlinks to a larger volume (a class the unit never runs), and uv 0.13 names its git folder ``git-v1`` and writes a ``.ok``
marker beside each checkout. A pass refuses a declaration only for a class it is asked to run; a class that is run keeps every
check it had, and a symlinked root is still refused whenever its class runs.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_host_retention_effect.handlers.handler_host_retention import (
    load_bounds,
)
from omnimarket.nodes.node_host_retention_effect.handlers.host_retention_ports import (
    FilesystemUvArchive,
)
from omnimarket.nodes.node_host_retention_effect.models.model_host_retention import (
    ModelUvArchiveEntry,
)
from tests.nodes.node_host_retention_effect.host_retention_fakes import (
    CONTAINER_WORK,
    NOW,
    FakeUv,
    FakeUvArchive,
    make_ports,
    run,
)

pytestmark = pytest.mark.unit

_GIB = 2**30


def _symlinked_runner_fields(tmp_path: Path) -> dict[str, Any]:
    """A runner group whose host roots sit under ``runners/mq-N``, a symlink to the real folder on another volume."""
    volume = tmp_path / "volume"
    (volume / "runners" / "mq-1" / "work").mkdir(parents=True)
    (tmp_path / "runners").mkdir()
    os.symlink(volume / "runners" / "mq-1", tmp_path / "runners" / "mq-1")
    return {
        "runner_groups": [
            {
                "ids": ["mq-1"],
                "container_template": "ci-runner-{id}",
                "container_user": "runner",
                "mounts": [
                    {
                        "host_root_template": str(
                            tmp_path / "runners" / "{id}" / "work"
                        ),
                        "container_root": CONTAINER_WORK,
                        "disposable_patterns": ["*"],
                        "keep_gib": 1,
                    }
                ],
            }
        ]
    }


def _uv_cache_fields(
    path: Path, buckets: tuple[str, ...] = ("archive-v0",)
) -> dict[str, Any]:
    return {
        "uv_caches": [
            {
                "path": str(path),
                "uv_binary": "uv",
                "min_gib": 20.0,
                "ci_prune": False,
                "timeout_seconds": 1800,
                "cap": {"cap_gib": 30.0, "buckets": list(buckets), "keep_days": 7.0},
            }
        ]
    }


def _uv_path(tmp_path: Path) -> Path:
    path = tmp_path / "uv-cache"
    path.mkdir()
    return path


def test_a_uv_cache_pass_is_not_refused_by_symlinked_runner_roots(
    tmp_path: Path,
) -> None:
    runner = _symlinked_runner_fields(tmp_path)
    fields = {**runner, **_uv_cache_fields(_uv_path(tmp_path))}

    result = run(tmp_path, fields, ports=make_ports(), classes=("uv_cache",))

    assert result.refusals == ()
    assert result.status == "ok"


@pytest.mark.parametrize("classes", [(), ("runner_work",)])
def test_the_symlinked_runner_roots_are_still_refused_when_their_class_runs(
    tmp_path: Path, classes: tuple[Any, ...]
) -> None:
    """Control: the same row, with the runner class (or every class) asked for, is refused for the symlink."""
    runner = _symlinked_runner_fields(tmp_path)
    fields = {**runner, **_uv_cache_fields(_uv_path(tmp_path))}

    result = run(tmp_path, fields, ports=make_ports(), classes=classes)

    assert result.status == "refused"
    assert any("is a symlink or sits under one" in text for text in result.refusals)
    assert result.items == ()


def test_a_symlinked_uv_cache_path_is_refused_when_the_uv_class_runs(
    tmp_path: Path,
) -> None:
    real = tmp_path / "volume" / "uv-cache"
    real.mkdir(parents=True)
    link = tmp_path / "uv-cache"
    os.symlink(real, link)

    result = run(
        tmp_path, _uv_cache_fields(link), ports=make_ports(), classes=("uv_cache",)
    )

    assert result.status == "refused"
    assert any(
        "uv cache path" in text and "symlink" in text for text in result.refusals
    )


def test_the_contract_names_the_uv_013_git_bucket_with_its_marker() -> None:
    buckets = load_bounds().uv_evictable_buckets

    assert buckets["git-v1/checkouts"].depth == 2
    assert buckets["git-v1/checkouts"].keep_linked is False
    assert buckets["git-v1/checkouts"].marker_suffix == ".ok"


def test_a_cap_over_the_git_v1_bucket_is_accepted_and_evicts_its_old_checkout(
    tmp_path: Path,
) -> None:
    path = _uv_path(tmp_path)
    archive = FakeUvArchive(
        entries={
            "git-v1/checkouts": [
                ModelUvArchiveEntry(
                    name="repo/abc1234",
                    bytes=10 * _GIB,
                    newest_mtime=NOW - 30 * 86400.0,
                    linked=False,
                    special=False,
                )
            ]
        }
    )
    ports = make_ports(uv=FakeUv(size=50 * _GIB, after=40 * _GIB), uv_archive=archive)

    result = run(
        tmp_path,
        _uv_cache_fields(path, buckets=("archive-v0", "git-v1/checkouts")),
        ports=ports,
        classes=("uv_cache",),
    )

    assert result.refusals == ()
    assert result.status == "ok"
    assert archive.removed == ["git-v1/checkouts/repo/abc1234"]
    assert archive.removed_markers == [".ok"]


def _git_v1_checkout(bucket: Path, repo: str, revision: str) -> Path:
    checkout = bucket / repo / revision
    checkout.mkdir(parents=True)
    (checkout / "file.txt").write_text("x", encoding="utf-8")
    (bucket / repo / f"{revision}.ok").write_bytes(b"")
    return checkout


def test_the_filesystem_scan_pairs_a_marker_with_its_checkout(tmp_path: Path) -> None:
    bucket = tmp_path / "git-v1" / "checkouts"
    _git_v1_checkout(bucket, "repo", "abc1234")
    (bucket / "repo" / "stray.ok").write_bytes(b"")

    entries = FilesystemUvArchive().scan(str(bucket), 2, 30.0, ".ok")

    by_name = {item.name: item for item in entries}
    assert set(by_name) == {"repo/abc1234", "repo/stray.ok"}
    assert by_name["repo/abc1234"].special is False
    assert (
        by_name["repo/stray.ok"].special is True
    )  # an orphan marker is kept, never removed


def test_the_filesystem_scan_without_a_marker_suffix_lists_the_marker_as_special(
    tmp_path: Path,
) -> None:
    """Control: with no suffix declared the marker is a special file beside the checkout."""
    bucket = tmp_path / "git-v1" / "checkouts"
    _git_v1_checkout(bucket, "repo", "abc1234")

    entries = FilesystemUvArchive().scan(str(bucket), 2, 30.0)

    assert {item.name for item in entries if item.special} == {"repo/abc1234.ok"}


def test_the_filesystem_removal_unlinks_the_marker_with_the_checkout(
    tmp_path: Path,
) -> None:
    bucket = tmp_path / "git-v1" / "checkouts"
    kept = _git_v1_checkout(bucket, "repo", "keep0001")
    gone = _git_v1_checkout(bucket, "repo", "gone0002")

    FilesystemUvArchive().remove(str(bucket), "repo/gone0002", ".ok")

    assert not gone.exists()
    assert not (bucket / "repo" / "gone0002.ok").exists()
    assert kept.exists()
    assert (bucket / "repo" / "keep0001.ok").exists()


def test_the_filesystem_removal_refuses_a_marker_that_is_not_a_regular_file(
    tmp_path: Path,
) -> None:
    bucket = tmp_path / "git-v1" / "checkouts"
    checkout = _git_v1_checkout(bucket, "repo", "abc1234")
    marker = bucket / "repo" / "abc1234.ok"
    marker.unlink()
    marker.mkdir()

    with pytest.raises(OSError, match="not a regular file"):
        FilesystemUvArchive().remove(str(bucket), "repo/abc1234", ".ok")

    assert checkout.exists()
