# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Inputs shared by the host disk command tests (OMN-23867): a made-up host with real directories under a tmp path.

``scenario_safety`` and ``scenario_golden`` hold the assertions of the safety and golden tests, so the controls
(test_disk_command_controls) run the same assertions against a build with a break injected and expect red.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env

from omnimarket.nodes.node_host_storage_effect.handlers.backend_subprocess import (
    BackendHostStorageSubprocess,
)
from omnimarket.nodes.node_host_storage_effect.handlers.entry_remover_local import (
    EntryRemoverLocal,
)
from omnimarket.nodes.node_host_storage_effect.handlers.handler_host_storage import (
    HandlerHostStorage,
)
from omnimarket.nodes.node_host_storage_effect.models.model_host_storage import (
    EnumHostStorageEntryOutcome,
    EnumHostStorageOutcome,
    ModelHostStorageRequest,
    ModelHostStorageResult,
)
from omnimarket.nodes.node_host_storage_effect.models.model_host_storage_overlay import (
    ModelHostStorageOverlay,
)
from omnimarket.nodes.node_temp_retention_effect.handlers.handler_temp_retention import (
    ProcessReferences,
)

NODE_DIR = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_host_storage_effect"
)
HOST = "pool-a"
CAPACITY = 10_000_000
LEASE = ".lane-lease"
# A deterministic "df": free bytes are the capacity less the size of every file under the path.
FREE_SCRIPT = (
    "import os,sys;"
    f"print({CAPACITY}-sum(os.path.getsize(os.path.join(d,f)) for d,_,fs in os.walk(sys.argv[1]) for f in fs))"
)
# The docker class stand-in: removes the one file named by its argument.
PRUNE_SCRIPT = "import os,sys;os.remove(sys.argv[1])"


@dataclass(frozen=True)
class Dirs:
    cache: Path
    worktrees: Path
    lab_runs: Path
    outside: Path
    docker_stand_in: Path


def make_dirs(tmp_path: Path) -> Dirs:
    dirs = Dirs(
        cache=tmp_path / "cache",
        worktrees=tmp_path / "worktrees",
        lab_runs=tmp_path / "lab-runs",
        outside=tmp_path / "outside",
        docker_stand_in=tmp_path / "docker",
    )
    for path in dirs.__dict__.values():
        path.mkdir(parents=True)
    (dirs.docker_stand_in / "layer.bin").write_bytes(b"d" * 3000)
    return dirs


def make_entry(parent: Path, name: str, size: int = 4000) -> Path:
    entry = parent / name
    entry.mkdir()
    (entry / "payload.bin").write_bytes(b"x" * size)
    return entry


def overlay_document(dirs: Dirs, *, backend: str = "subprocess") -> dict[str, Any]:
    return {
        "backend": backend,
        "free_bytes_argv": [sys.executable, "-c", FREE_SCRIPT, "{path}"],
        "free_bytes_timeout_seconds": 10,
        "commands": [
            {
                "name": "evict_cache",
                "kind": "remove_entries",
                "measured_path": str(dirs.cache),
                "timeout_seconds": 30,
            },
            {
                "name": "remove_worktrees",
                "kind": "remove_worktrees",
                "measured_path": str(dirs.worktrees),
                "timeout_seconds": 30,
                "lease_marker_names": [LEASE],
            },
            {
                "name": "remove_lab_runs",
                "kind": "remove_entries",
                "measured_path": str(dirs.lab_runs),
                "timeout_seconds": 30,
            },
            {
                "name": "prune_docker_build_cache",
                "argv": [
                    sys.executable,
                    "-c",
                    PRUNE_SCRIPT,
                    str(dirs.docker_stand_in / "layer.bin"),
                ],
                "measured_path": str(dirs.docker_stand_in),
                "timeout_seconds": 30,
            },
        ],
        "hosts": [
            {
                "name": HOST,
                "transport_prefix": [],
                "commands": [
                    "evict_cache",
                    "remove_worktrees",
                    "remove_lab_runs",
                    "prune_docker_build_cache",
                ],
                "entry_roots": [
                    {"command": "evict_cache", "path": str(dirs.cache)},
                    {"command": "remove_worktrees", "path": str(dirs.worktrees)},
                    {"command": "remove_lab_runs", "path": str(dirs.lab_runs)},
                ],
            },
            {
                "name": "pool-b",
                "transport_prefix": [],
                "commands": ["prune_docker_build_cache"],
            },
        ],
    }


def write_overlay_root(tmp_path: Path, document: dict[str, Any]) -> Path:
    """The overlay root: ``<root>/node_host_storage_effect/overlay.yaml``, as the overlays repository lays it out."""
    node_dir = tmp_path / "overlay-root" / "node_host_storage_effect"
    node_dir.mkdir(parents=True)
    (node_dir / "overlay.yaml").write_text(yaml.safe_dump(document), encoding="utf-8")
    return tmp_path / "overlay-root"


def real_handler(dirs: Dirs, *, no_holders: bool = False) -> HandlerHostStorage:
    """The handler on the real subprocess backend; ``no_holders`` stands in a quiet process table."""
    overlay = ModelHostStorageOverlay.model_validate(overlay_document(dirs))
    remover = EntryRemoverLocal(
        process_references=(lambda: ProcessReferences()) if no_holders else None
    )
    return HandlerHostStorage(
        overlay,
        BackendHostStorageSubprocess(
            overlay.free_bytes_argv, overlay.free_bytes_timeout_seconds, remover
        ),
    )


def request(
    command: str, *entries: Path | str, host: str = HOST
) -> ModelHostStorageRequest:
    return ModelHostStorageRequest(
        host=host,
        command=command,
        requested_by="test-lane",
        entries=tuple(str(e) for e in entries),
    )


def by_entry(result: ModelHostStorageResult) -> dict[str, EnumHostStorageEntryOutcome]:
    return {r.entry: r.outcome for r in result.entry_results}


@contextmanager
def holders(cwd_in: Path, file_in: Path) -> Iterator[None]:
    """Two real processes: one whose working directory is inside ``cwd_in``, one holding ``file_in`` open.

    The path of the held file reaches the second process only as an inherited descriptor, never in its
    command line or environment, so the scan finds it through the descriptor alone.
    """
    sleeper = "import time;time.sleep(120)"
    fd = os.open(file_in, os.O_RDONLY)
    try:
        by_fd = subprocess.Popen(
            [sys.executable, "-c", sleeper],
            pass_fds=(fd,),
            stdin=subprocess.DEVNULL,
        )
    finally:
        os.close(fd)
    by_cwd = subprocess.Popen(
        [sys.executable, "-c", sleeper], cwd=cwd_in, stdin=subprocess.DEVNULL
    )
    try:
        yield
    finally:
        for proc in (by_fd, by_cwd):
            proc.terminate()
            proc.wait(timeout=10)


def git(repo: Path, *args: str) -> None:
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.invalid",
            *args,
        ],
        check=True,
        env=scrub_git_location_env(os.environ),
        capture_output=True,
        text=True,
    )


def make_worktree(dirs: Dirs, name: str) -> Path:
    """A real linked git worktree under the worktrees root."""
    repo = dirs.outside / "repo"
    if not repo.exists():
        repo.mkdir()
        git(repo, "init", "-q")
        (repo / "f.txt").write_text("f", encoding="utf-8")
        git(repo, "add", "f.txt")
        git(repo, "commit", "-q", "-m", "init")
    git(repo, "worktree", "add", "-q", "-b", name, str(dirs.worktrees / name))
    return dirs.worktrees / name


def scenario_safety(tmp_path: Path) -> None:
    """AC2: outside-root and in-use entries are refused and survive; the free ones are removed."""
    dirs = make_dirs(tmp_path)
    free_a = make_entry(dirs.cache, "pkg-free-a")
    free_b = make_entry(dirs.cache, "pkg-free-b")
    held_fd = make_entry(dirs.cache, "pkg-held-by-fd")
    held_cwd = make_entry(dirs.cache, "pkg-held-by-cwd")
    secret = make_entry(dirs.outside, "secret")
    (dirs.cache / "link-out").symlink_to(dirs.outside)
    (dirs.cache / "link-secret").symlink_to(secret)
    handler = real_handler(dirs)
    with holders(held_cwd, held_fd / "payload.bin"):
        result = handler.handle(
            request(
                "evict_cache",
                free_a,
                free_b,
                held_fd,
                held_cwd,
                secret,
                dirs.cache / "link-out" / "secret",
                dirs.cache / "link-secret",
                dirs.cache / ".." / "outside" / "secret",
                "pkg-free-a",
                dirs.cache,
            )
        )
    out = EnumHostStorageEntryOutcome
    assert by_entry(result) == {
        str(free_a): out.REMOVED,
        str(free_b): out.REMOVED,
        str(held_fd): out.REFUSED_IN_USE,
        str(held_cwd): out.REFUSED_IN_USE,
        str(secret): out.REFUSED_OUTSIDE_ROOT,
        str(dirs.cache / "link-out" / "secret"): out.REFUSED_OUTSIDE_ROOT,
        str(dirs.cache / "link-secret"): out.REFUSED_OUTSIDE_ROOT,
        str(dirs.cache / ".." / "outside" / "secret"): out.REFUSED_OUTSIDE_ROOT,
        "pkg-free-a": out.REFUSED_OUTSIDE_ROOT,
        str(dirs.cache): out.REFUSED_OUTSIDE_ROOT,
    }
    assert result.outcome is EnumHostStorageOutcome.PARTIAL
    assert not free_a.exists()
    assert not free_b.exists()
    for survivor in (held_fd, held_cwd, secret, dirs.cache):
        assert survivor.exists(), survivor
    assert (secret / "payload.bin").read_bytes() == b"x" * 4000
    assert (dirs.cache / "link-secret").is_symlink()


def scenario_worktrees(tmp_path: Path) -> None:
    """AC2: a real worktree with a lease is refused; a real free worktree is removed."""
    dirs = make_dirs(tmp_path)
    leased = make_worktree(dirs, "wt-leased")
    free = make_worktree(dirs, "wt-free")
    (leased / LEASE).write_text("lane-x", encoding="utf-8")
    result = real_handler(dirs, no_holders=True).handle(
        request("remove_worktrees", leased, free)
    )
    assert by_entry(result) == {
        str(leased): EnumHostStorageEntryOutcome.REFUSED_IN_USE,
        str(free): EnumHostStorageEntryOutcome.REMOVED,
    }
    assert leased.exists()
    assert not free.exists()


def scenario_golden(tmp_path: Path) -> ModelHostStorageResult:
    """AC4/AC5: a command, entries removed, a typed result with the freed bytes."""
    dirs = make_dirs(tmp_path)
    first = make_entry(dirs.cache, "pkg-a", size=40_000)
    second = make_entry(dirs.cache, "pkg-b", size=60_000)
    result = real_handler(dirs, no_holders=True).handle(
        request("evict_cache", first, second)
    )
    assert result.outcome is EnumHostStorageOutcome.OK
    assert [r.outcome for r in result.entry_results] == [
        EnumHostStorageEntryOutcome.REMOVED
    ] * 2
    assert result.free_bytes_before == CAPACITY - 100_000
    assert result.free_bytes_after == CAPACITY
    assert result.freed_bytes == 100_000
    assert not first.exists()
    assert not second.exists()
    return result
