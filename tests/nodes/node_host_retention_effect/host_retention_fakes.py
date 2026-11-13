# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Fake ports and builders shared by the host retention tests. Every host, path and number is fixture data."""

from __future__ import annotations

import os
import shutil
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal

import yaml

from omnimarket.nodes.node_host_retention_effect.handlers.handler_host_retention import (
    HandlerHostRetention,
)
from omnimarket.nodes.node_host_retention_effect.handlers.host_retention_ports import (
    HostRetentionPorts,
)
from omnimarket.nodes.node_host_retention_effect.models.model_host_retention import (
    ModelDiskReading,
    ModelHostRetentionRequest,
    ModelHostRetentionResult,
    ModelUvArchiveEntry,
)
from omnimarket.nodes.node_temp_retention_effect.handlers.handler_temp_retention import (
    ProcessReferences,
)

NOW = 2_000_000_000.0
DAY = 86400.0
UID = os.getuid()
CONTAINER_WORK = "actions-runner/_work"
UNREADABLE: Any = object()


class FakeRunnerState:
    """Idle until it has been read ``flips_after`` times, then busy; ``idle=None`` is an unreadable state."""

    def __init__(
        self, idle: bool | None = True, flips_after: int | None = None
    ) -> None:
        self.idle = idle
        self.flips_after = flips_after
        self.reads = 0

    def is_idle(self, container: str) -> bool | None:
        self.reads += 1
        if self.flips_after is not None and self.reads > self.flips_after:
            return False
        return self.idle


class FakeRunnerRemove:
    """Removes the host copy of the container path it is given, as a bind mount would."""

    def __init__(self, host_root: Path | None = None, fail: bool = False) -> None:
        self.host_root = host_root
        self.fail = fail
        self.calls: list[tuple[str, str]] = []

    def remove(self, container: str, user: str, home_relative_path: str) -> None:
        container_path = home_relative_path
        self.calls.append((container, container_path))
        if self.fail:
            raise OSError("exec refused")
        assert self.host_root is not None
        shutil.rmtree(self.host_root / Path(container_path).name)


class FakeUv:
    def __init__(self, size: int = 0, after: int = 0, fail: bool = False) -> None:
        self.sizes = [size, after]
        self.fail = fail
        self.prunes: list[tuple[str, str, bool]] = []

    def size_bytes(self, uv_binary: str, cache_dir: str, timeout_s: float) -> int:
        return self.sizes.pop(0) if len(self.sizes) > 1 else self.sizes[0]

    def prune(self, uv_binary: str, cache_dir: str, ci: bool, timeout_s: float) -> None:
        if self.fail:
            raise OSError("uv refused")
        self.prunes.append((uv_binary, cache_dir, ci))


class FakeUvArchive:
    """The uv archive port. ``entries`` is a list (the ``archive-v0`` bucket) or a dict bucket -> list; the port records each scan
    (bucket folder, depth) and each removed ``bucket/name``, can fail a scan (all, or the buckets named in ``fail_buckets``) or a
    remove, and really deletes the folder when given a cache ``root``."""

    def __init__(
        self,
        entries: list[ModelUvArchiveEntry]
        | dict[str, list[ModelUvArchiveEntry]]
        | None = None,
        *,
        fail_scan: bool = False,
        fail_buckets: tuple[str, ...] = (),
        fail_remove: bool = False,
        root: Path | None = None,
    ) -> None:
        if entries is None:
            entries = {}
        self.entries = entries if isinstance(entries, dict) else {"archive-v0": entries}
        self.fail_scan = fail_scan
        self.fail_buckets = fail_buckets
        self.fail_remove = fail_remove
        self.root = root
        self.scans: list[tuple[str, int]] = []
        self.removed: list[str] = []

    def _bucket(self, bucket_dir: str) -> str:
        for bucket in self.entries:
            if bucket_dir.endswith("/" + bucket):
                return bucket
        return ""

    def scan(
        self, bucket_dir: str, depth: int, timeout_s: float
    ) -> list[ModelUvArchiveEntry]:
        self.scans.append((bucket_dir, depth))
        bucket = self._bucket(bucket_dir)
        if (
            self.fail_scan
            or (bucket and bucket in self.fail_buckets)
            or any(bucket_dir.endswith("/" + name) for name in self.fail_buckets)
        ):
            raise OSError("scan refused")
        return list(self.entries.get(bucket, []))

    def remove(self, bucket_dir: str, name: str) -> None:
        if self.fail_remove:
            raise OSError("remove refused")
        self.removed.append(f"{self._bucket(bucket_dir)}/{name}")
        if self.root is not None:
            shutil.rmtree(self.root / name, ignore_errors=True)


class FakeLanes:
    def __init__(self, lanes: set[str] | None) -> None:
        self.lanes = lanes

    def live_lanes(
        self, run_roots: Sequence[str], receipt_roots: Sequence[str]
    ) -> set[str] | None:
        return self.lanes


class FakeMounts:
    def __init__(self, sources: set[str] | None) -> None:
        self.sources = sources

    def mount_sources(self) -> set[str] | None:
        return self.sources


class FakeUnits:
    def __init__(self, texts: list[str] | None) -> None:
        self.texts = texts

    def unit_texts(self, roots: Sequence[str]) -> list[str] | None:
        return self.texts


class FakePreCommitStore:
    """Remembers every ``forget`` call; ``fail`` raises OSError, else the declared result."""

    def __init__(
        self,
        result: Literal["forgotten", "unreferenced", "locked"] = "forgotten",
        fail: bool = False,
    ) -> None:
        self.result = result
        self.fail = fail
        self.calls: list[tuple[str, str]] = []

    def forget(
        self, index_db: str, repo_path: str
    ) -> Literal["forgotten", "unreferenced", "locked"]:
        self.calls.append((index_db, repo_path))
        if self.fail:
            raise OSError("index refused")
        return self.result


class FakeDisk:
    def __init__(self) -> None:
        self.reads = 0

    def reading(self, path: str) -> ModelDiskReading:
        self.reads += 1
        used = 80.0 if self.reads == 1 else 50.0
        return ModelDiskReading(
            path=path, used_bytes=int(used), total_bytes=100, used_percent=used
        )


def make_ports(
    *,
    runner_state: Any = None,
    runner_remove: Any = None,
    uv: Any = None,
    uv_archive: Any = None,
    lanes: Any = None,
    mounts: Any = None,
    units: Any = None,
    pre_commit: Any = None,
) -> HostRetentionPorts:
    return HostRetentionPorts(
        runner_state=runner_state or FakeRunnerState(),
        runner_remove=runner_remove or FakeRunnerRemove(),
        uv_cache=uv or FakeUv(),
        uv_archive=uv_archive or FakeUvArchive(),
        lane_liveness=lanes or FakeLanes(set()),
        container_mounts=mounts or FakeMounts(set()),
        unit_files=units or FakeUnits([]),
        disk=FakeDisk(),
        pre_commit_store=pre_commit or FakePreCommitStore(),
    )


def age(path: Path, days: float) -> None:
    """Set the modification time of a path and everything under it to ``days`` before NOW."""
    stamp = NOW - days * DAY
    for current, dirs, files in os.walk(path, topdown=False, followlinks=False):
        for name in [*dirs, *files]:
            os.utime(os.path.join(current, name), (stamp, stamp), follow_symlinks=False)
    os.utime(path, (stamp, stamp), follow_symlinks=False)


def entry(root: Path, name: str, *, days: float, files: int = 1) -> Path:
    """A directory entry of ``root`` holding ``files`` 4 KiB files, last changed ``days`` ago."""
    path = root / name
    path.mkdir(parents=True)
    for number in range(files):
        (path / f"f{number}").write_bytes(b"x" * 4096)
    age(path, days)
    return path


def scratch_class(root: Path, keep_days: float = 3, **fields: Any) -> dict[str, Any]:
    return {
        "name": "scratch-a",
        "kind": "lane_scratch",
        "root": str(root),
        "keep_days": keep_days,
        **fields,
    }


def precommit_class(root: Path, keep_days: float = 7, **fields: Any) -> dict[str, Any]:
    return {
        "name": "pc-store",
        "kind": "pre_commit_store",
        "root": str(root),
        "index_db": str(root / "db.db"),
        "keep_days": keep_days,
        **fields,
    }


def overlay_file(tmp_path: Path, **host_fields: Any) -> str:
    path = tmp_path / "overlay.yaml"
    path.write_text(
        yaml.safe_dump(
            {"hosts": [{"name": "pool-a", "docker_timeout_seconds": 5, **host_fields}]}
        )
    )
    return str(path)


def run(
    tmp_path: Path,
    host_fields: dict[str, Any],
    *,
    ports: HostRetentionPorts | None = None,
    refs: Any = None,
    dry_run: bool = False,
    classes: tuple[Any, ...] = (),
    uid: int = UID,
) -> ModelHostRetentionResult:
    references = ProcessReferences() if refs is None else refs
    handler = HandlerHostRetention(
        now=lambda: NOW,
        references=lambda: None if references is UNREADABLE else references,
        ports=ports or make_ports(),
        uid=uid,
    )
    return handler.handle(
        ModelHostRetentionRequest(
            host_name="pool-a",
            overlay_file=overlay_file(tmp_path, **host_fields),
            dry_run=dry_run,
            classes=classes,
        )
    )
