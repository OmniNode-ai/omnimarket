# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ports and real adapters for the host retention node.

Every external effect (docker, uv, the filesystem and the lane liveness scan) is behind a port so
that tests inject fakes. The real adapters shell out with an argv list (never ``shell=True``),
capture output and time out after 120 seconds.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal, Protocol

from omnimarket.handlers.ledger_row_grammar import parse_row
from omnimarket.nodes.node_host_retention_effect.models.model_host_retention import (
    ModelDiskReading,
    ModelLedgerLoader,
    ModelUvArchiveEntry,
)
from omnimarket.nodes.node_retention_cleanup.handlers.handler_file_retention import (
    _ensure_archive,
    _has_marker,
    _is_protected,
    _verify_archive,
    load_file_policy,
)
from omnimarket.nodes.node_temp_retention_effect.handlers.handler_temp_retention import (
    ProcessReferences,
    read_process_references,
)


def _run(
    argv: list[str], *, timeout_s: float, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run one argv; a missing binary or a timeout is an OSError, never a crash of the pass."""
    try:
        return subprocess.run(
            argv,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            env=env,
        )
    except subprocess.TimeoutExpired:
        raise OSError(f"{argv[0]} {argv[1]}: no answer in {timeout_s:.0f} s") from None
    except FileNotFoundError:
        raise OSError(f"{argv[0]}: not installed") from None


def read_references_portable() -> ProcessReferences | None:
    """The live process references, read from ``/proc`` or, without it, from ``ps``.

    The command line of a process names any path it works in as an argument; a working directory
    alone is not seen on macOS.
    """
    found = read_process_references()
    if found is not None:
        return found
    try:
        proc = _run(["ps", "-axww", "-o", "command="], timeout_s=20)
    except OSError:
        return None
    if proc.returncode != 0:
        return None
    lines = [line for line in proc.stdout.splitlines() if line]
    return ProcessReferences(blobs=list(lines), commands=list(lines))


class CiHostStatePort(Protocol):
    """Whether a runner container is idle."""

    def is_idle(self, container: str) -> bool | None:  # None = state could not be read
        ...


class CiHostRemovePort(Protocol):
    """Remove a path under the home of a runner container user."""

    def remove(
        self, container: str, user: str, home_relative_path: str
    ) -> None:  # raises OSError on failure
        ...


class UvCachePort(Protocol):
    """Measure and prune a uv cache."""

    def size_bytes(self, uv_binary: str, cache_dir: str, timeout_s: float) -> int: ...

    def prune(
        self, uv_binary: str, cache_dir: str, ci: bool, timeout_s: float
    ) -> None:  # raises OSError on failure
        ...


class UvArchivePort(Protocol):
    """Scan and remove whole entries of a uv cache bucket."""

    def scan(
        self, bucket_dir: str, depth: int, timeout_s: float
    ) -> list[ModelUvArchiveEntry]:
        """List entries; raises OSError on failure or past the deadline."""

    def remove(self, bucket_dir: str, name: str) -> None:
        """Remove one entry; raises OSError on failure."""


class LaneLivenessPort(Protocol):
    """Which lanes are live."""

    def live_lanes(
        self, run_roots: Sequence[str], receipt_roots: Sequence[str]
    ) -> set[str] | None:  # None = unreadable
        ...


_DATED_ROW_TYPES = frozenset({"CLAIM", "TERMINAL"})
_LOADER_ANSWERED_EXITS = (
    0,
    10,
)  # the rows loader's node answer and its counted file fallback


class LaneClosurePort(Protocol):
    """The newest CLAIM and TERMINAL of each lane, read from the rolling work ledger."""

    def lane_states(
        self, loader: ModelLedgerLoader
    ) -> dict[str, LaneLedgerState] | None:
        """lane -> epoch seconds of its newest CLAIM and of its newest TERMINAL in the
        window; None when the ledger could not be read."""


@dataclass(frozen=True)
class ReceiptReading:
    """What the receipt files matched in one lane's receipt root say.

    ``files`` counts every matched path; ``unreadable`` counts those that could not be parsed or are
    not plain regular files; ``not_final`` counts parsed receipts whose JSON ``final`` is false;
    ``newest_mtime`` is the newest mtime over the matched files, 0.0 when there are none.
    """

    files: int
    unreadable: int
    not_final: int
    newest_mtime: float


class LaneReceiptsPort(Protocol):
    """Read the receipt files of one lane."""

    def read(self, root: str, pattern: str) -> ReceiptReading: ...


class ContainerMountPort(Protocol):
    """The host paths that running containers bind-mount."""

    def mount_sources(self) -> set[str] | None:  # None = unreadable
        ...


class UnitFilesPort(Protocol):
    """The text of every installed unit file under the declared unit directories."""

    def unit_texts(self, roots: Sequence[str]) -> list[str] | None:  # None = unreadable
        ...


class DiskPort(Protocol):
    """Read disk usage for a path."""

    def reading(self, path: str) -> ModelDiskReading: ...


class PreCommitStorePort(Protocol):
    """Forget a repository path in the pre-commit store's index.

    Takes the store lock, deletes the rows whose path is repo_path; "locked" when the lock is
    held by another process; "unreferenced" when no row named the path.
    """

    def forget(
        self, index_db: str, repo_path: str
    ) -> Literal["forgotten", "unreferenced", "locked"]: ...


class ArchivePort(Protocol):
    """Verified copies of aging entries, written outside the trees the pass removes."""

    def root(self) -> Path | None:
        """The archive root, or None when none is declared or it is not a directory."""

    def archive(self, source: Path, dest: Path) -> int:
        """Leave a verified copy of ``source`` at ``dest``; return its size in bytes."""

    def verify(self, source: Path, dest: Path) -> None:
        """Raise OSError when the copy at ``dest`` does not match ``source``."""

    def synced(self, dest: Path) -> bool:
        """True when the copy at ``dest`` is known to have reached its sync target."""

    def archivable(self, source: Path) -> bool:
        """True when everything under ``source`` goes into the copy: no symlink and nothing the archive code leaves out."""


class DockerCiHostState:
    """A runner is idle when no ``Runner.Worker`` process is running in the container."""

    def __init__(self, timeout_s: float) -> None:
        self.timeout_s = timeout_s

    def is_idle(self, container: str) -> bool | None:
        try:
            proc = _run(
                ["docker", "top", container, "-eo", "pid,args"],
                timeout_s=self.timeout_s,
            )
        except OSError:
            return None
        if proc.returncode != 0:
            return None
        return all("Runner.Worker" not in line for line in proc.stdout.splitlines())


class DockerCiHostRemove:
    """Remove one path under a runner container user's home, as that user, after refusing unsafe paths.

    The path is passed to ``sh`` as an argument (never spliced into the script) and joined to ``$HOME`` inside the container.
    """

    def __init__(self, timeout_s: float) -> None:
        self.timeout_s = timeout_s

    def remove(self, container: str, user: str, home_relative_path: str) -> None:
        path = home_relative_path
        parts = [part for part in path.split("/") if part]
        if not path or path.startswith("/"):
            raise OSError(f"{path!r} is not a path relative to the home")
        if os.path.normpath(path) != path or ".." in parts:
            raise OSError(f"{path!r} is not normalised or has a '..' component")
        if len(parts) < 2:
            raise OSError(f"{path!r} has fewer than two components")
        proc = _run(
            [
                "docker",
                "exec",
                "--user",
                user,
                container,
                "sh",
                "-c",
                'rm -rf -- "$HOME/$1"',
                "sh",
                path,
            ],
            timeout_s=self.timeout_s,
        )
        if proc.returncode != 0:
            raise OSError(proc.stderr[:200])


class ProcessUvCache:
    """Measure and prune a uv cache with the ``uv`` binary."""

    def size_bytes(self, uv_binary: str, cache_dir: str, timeout_s: float) -> int:
        env = {**os.environ, "UV_CACHE_DIR": cache_dir}
        proc = _run(
            [os.path.expanduser(uv_binary), "cache", "size"],
            timeout_s=timeout_s,
            env=env,
        )
        if proc.returncode != 0:
            raise OSError(proc.stderr[:200])
        match = re.search(r"\d+", proc.stdout)
        if not match:
            raise OSError("no integer in uv cache size output")
        return int(match.group(0))

    def prune(self, uv_binary: str, cache_dir: str, ci: bool, timeout_s: float) -> None:
        env = {**os.environ, "UV_CACHE_DIR": cache_dir}
        argv = [os.path.expanduser(uv_binary), "cache", "prune"]
        if ci:
            argv.append("--ci")
        proc = _run(argv, timeout_s=timeout_s, env=env)
        if proc.returncode != 0:
            raise OSError(proc.stderr[:200])


class FilesystemUvArchive:
    """Scan and remove whole entries of a uv cache bucket on the filesystem.

    An entry is a folder ``depth`` levels under the bucket (1: ``archive-v0/<id>``; 2: ``git-v0/checkouts/<repo>/<rev>``),
    named by its path relative to the bucket. Nothing is followed through a symlink.
    """

    def scan(
        self, bucket_dir: str, depth: int, timeout_s: float
    ) -> list[ModelUvArchiveEntry]:
        """Every entry of the bucket; an unreadable bucket or a scan past the deadline raises OSError, nothing is partial."""
        deadline = time.monotonic() + timeout_s
        entries: list[ModelUvArchiveEntry] = []

        def check_time() -> None:
            if time.monotonic() > deadline:
                raise OSError(f"{bucket_dir}: scan past the deadline")

        def special(name: str) -> ModelUvArchiveEntry:
            return ModelUvArchiveEntry(
                name=name, bytes=0, newest_mtime=0.0, linked=False, special=True
            )

        def measure(path: str, name: str) -> ModelUvArchiveEntry:
            total = 0
            newest = os.lstat(path).st_mtime
            linked = False
            for root, dirs, files in os.walk(path, followlinks=False):
                check_time()
                for child in files:
                    info = os.lstat(os.path.join(root, child))
                    total += info.st_size
                    newest = max(newest, info.st_mtime)
                    if stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
                        linked = True
                for child in dirs:
                    newest = max(newest, os.lstat(os.path.join(root, child)).st_mtime)
            return ModelUvArchiveEntry(
                name=name, bytes=total, newest_mtime=newest, linked=linked
            )

        def visit(path: str, relative: str, level: int) -> None:
            with os.scandir(path) as listing:
                children = sorted(listing, key=lambda item: item.name)
            for child in children:
                check_time()
                name = f"{relative}/{child.name}" if relative else child.name
                if not child.is_dir(follow_symlinks=False):
                    entries.append(special(name))
                elif level >= depth:
                    entries.append(measure(child.path, name))
                else:
                    visit(child.path, name, level + 1)

        visit(bucket_dir, "", 1)
        return entries

    def remove(self, bucket_dir: str, name: str) -> None:
        parts = name.split("/")
        if len(parts) > 3 or any(part in ("", ".", "..") for part in parts):
            raise OSError(f"{name!r} is not an entry path inside the bucket")
        target = os.path.join(bucket_dir, *parts)
        for level in range(1, len(parts) + 1):
            if os.path.islink(os.path.join(bucket_dir, *parts[:level])):
                raise OSError(f"{name!r} is or sits under a symlink")
        if not os.path.isdir(target):
            raise OSError(f"{name!r} is not a directory")
        shutil.rmtree(target)


class FilesystemLaneLiveness:
    """A lane is live when a run context holds a running pid, or a receipt is not final."""

    def live_lanes(
        self, run_roots: Sequence[str], receipt_roots: Sequence[str]
    ) -> set[str] | None:
        lanes: set[str] = set()
        for root in run_roots:
            path = Path(root)
            if not path.is_dir():
                continue
            try:
                entries = list(path.iterdir())
            except OSError:
                return None
            for entry in entries:
                name = entry.name
                if not name.startswith("rlane-"):
                    continue
                body = name[len("rlane-") :]
                if body.endswith(".ctx"):
                    body = body[:-4]
                if len(body) < 8 or body[-7] != "-":
                    continue
                if not all(c in "0123456789abcdef" for c in body[-6:]):
                    continue
                lane = body[:-7]
                pid_file = entry / "run.pid"
                if not pid_file.is_file():
                    continue
                try:
                    pid = int(pid_file.read_text(encoding="utf-8").strip())
                except (OSError, ValueError):
                    continue
                try:
                    os.kill(pid, 0)
                except PermissionError:
                    pass
                except OSError:
                    continue
                lanes.add(lane)
        for root in receipt_roots:
            path = Path(root)
            if not path.is_dir():
                continue
            try:
                entries = list(path.iterdir())
            except OSError:
                return None
            for entry in entries:
                if not entry.is_dir():
                    continue
                lane = entry.name
                try:
                    receipts = list(entry.glob("*.json"))
                except OSError:
                    return None
                for receipt in receipts:
                    try:
                        document = json.loads(receipt.read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        continue
                    if isinstance(document, dict) and document.get("final") is False:
                        lanes.add(lane)
                        break
        return lanes


@dataclass(frozen=True)
class LaneLedgerState:
    """The newest CLAIM and the newest TERMINAL of one lane, in epoch seconds.

    ``None`` when the lane has no row of that kind in the window.
    """

    claim: float | None
    terminal: float | None


def fold_lane_states(lines: Iterable[str]) -> dict[str, LaneLedgerState]:
    """The newest CLAIM and TERMINAL of every lane the ledger rows name, sorted by lane.

    The ledger's own row grammar reads each row; only a CLAIM or TERMINAL row that names its lane in a
    ``lane=<token>`` cell and carries a zoned stamp dates a lane. Every lane with any CLAIM or
    TERMINAL row of the window appears, whether or not it is closed.
    """
    states: dict[str, LaneLedgerState] = {}
    for line in lines:
        row = parse_row(line, types=_DATED_ROW_TYPES)
        if row is None:
            continue
        lane = row.token("lane")
        if lane is None:
            continue
        try:
            stamp = datetime.fromisoformat(row.stamp.replace("Z", "+00:00"))
        except ValueError:
            continue
        if stamp.tzinfo is None:
            continue
        current = states.get(lane)
        claim = None if current is None else current.claim
        terminal = None if current is None else current.terminal
        epoch = stamp.timestamp()
        if row.row_type == "CLAIM":
            claim = epoch if claim is None else max(claim, epoch)
        else:
            terminal = epoch if terminal is None else max(terminal, epoch)
        states[lane] = LaneLedgerState(claim=claim, terminal=terminal)
    return {lane: states[lane] for lane in sorted(states)}


class LoaderLaneClosure:
    """Run the ledger rows loader the overlay declares and fold the rows it prints.

    The loader is the one sanctioned ledger reader (it reads the bus-fed projection); this adapter opens
    no ledger file. Exit 0 (the node answered) and exit 10 (the loader's own counted file fallback
    answered) carry rows; any other exit, a timeout or a missing binary is an unreadable ledger.
    """

    def lane_states(
        self, loader: ModelLedgerLoader
    ) -> dict[str, LaneLedgerState] | None:
        argv = [
            *loader.argv,
            "--reader",
            loader.reader,
            "--window-hours",
            str(loader.window_hours),
        ]
        try:
            proc = _run(argv, timeout_s=loader.timeout_seconds)
        except OSError:
            return None
        if proc.returncode not in _LOADER_ANSWERED_EXITS:
            return None
        return fold_lane_states(proc.stdout.splitlines())


class FilesystemLaneReceipts:
    """Read the receipt files of one lane from the filesystem.

    A receipt whose JSON ``final`` is true says the run finished; ``final`` false counts toward
    ``not_final``. A parse error, a non-object body or a missing or non-bool ``final`` is unreadable,
    as is a matched path that is a symlink or not a regular file; nothing over 1 MiB is read.
    """

    _MAX_BYTES = 1 << 20

    def read(self, root: str, pattern: str) -> ReceiptReading:
        base = Path(os.path.expanduser(root))
        if not base.is_dir():
            return ReceiptReading(files=0, unreadable=0, not_final=0, newest_mtime=0.0)
        files = 0
        unreadable = 0
        not_final = 0
        newest = 0.0
        for match in base.glob(pattern):
            files += 1
            try:
                info = match.lstat()
            except OSError:
                unreadable += 1
                continue
            newest = max(newest, info.st_mtime)
            if (
                match.is_symlink()
                or not match.is_file()
                or info.st_size > self._MAX_BYTES
            ):
                unreadable += 1
                continue
            try:
                document = json.loads(match.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                unreadable += 1
                continue
            if not isinstance(document, dict) or not isinstance(
                document.get("final"), bool
            ):
                unreadable += 1
                continue
            if not document["final"]:
                not_final += 1
        return ReceiptReading(
            files=files, unreadable=unreadable, not_final=not_final, newest_mtime=newest
        )


class DockerContainerMounts:
    """Every bind-mount source of every running container, read with ``docker ps`` and ``docker inspect``."""

    def __init__(self, timeout_s: float) -> None:
        self.timeout_s = timeout_s

    def mount_sources(self) -> set[str] | None:
        try:
            listing = _run(["docker", "ps", "-q"], timeout_s=self.timeout_s)
        except OSError:
            return None
        if listing.returncode != 0:
            return None
        ids = listing.stdout.split()
        if not ids:
            return set()
        try:
            inspected = _run(
                [
                    "docker",
                    "inspect",
                    "--format",
                    "{{range .Mounts}}{{.Source}}{{println}}{{end}}",
                    *ids,
                ],
                timeout_s=self.timeout_s,
            )
        except OSError:
            return None
        if inspected.returncode != 0:
            return None
        return {line for line in inspected.stdout.splitlines() if line.startswith("/")}


class FilesystemUnitFiles:
    """Unit files and their drop-ins, read as text; a declared directory that exists but cannot be read is unreadable."""

    SUFFIXES = (".service", ".timer", ".path", ".socket", ".mount", ".conf")

    def unit_texts(self, roots: Sequence[str]) -> list[str] | None:
        texts: list[str] = []
        for root in roots:
            base = Path(root)
            if not base.is_dir():
                continue
            try:
                for current, _dirs, files in os.walk(base, followlinks=False):
                    for name in files:
                        if name.endswith(self.SUFFIXES):
                            with contextlib.suppress(OSError, UnicodeError):
                                texts.append(
                                    (Path(current) / name).read_text(encoding="utf-8")
                                )
            except OSError:
                return None
        return texts


class ShutilDisk:
    """Disk usage via ``shutil.disk_usage``."""

    def reading(self, path: str) -> ModelDiskReading:
        usage = shutil.disk_usage(path)
        return ModelDiskReading(
            path=path,
            used_bytes=usage.used,
            total_bytes=usage.total,
            used_percent=100.0 * usage.used / usage.total if usage.total else 0.0,
            free_bytes=usage.free,
        )


class SqlitePreCommitStore:
    """The pre-commit store's sqlite index, taken under the ``.lock`` file beside it.

    The row for a hook repository folder goes before the folder does, so pre-commit never finds a
    row whose folder is gone.
    """

    def forget(
        self, index_db: str, repo_path: str
    ) -> Literal["forgotten", "unreferenced", "locked"]:
        if not os.path.exists(index_db):
            raise OSError("index_db absent")
        lock_path = os.path.join(os.path.dirname(index_db), ".lock")
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return "locked"
            try:
                connection = sqlite3.connect(index_db, timeout=5)
                try:
                    cursor = connection.execute(
                        "DELETE FROM repos WHERE path = ?", (repo_path,)
                    )
                    connection.commit()
                    return "forgotten" if cursor.rowcount > 0 else "unreferenced"
                finally:
                    connection.close()
            except sqlite3.Error as exc:
                raise OSError(str(exc)[:200]) from exc
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)


class RetentionCleanupArchive:
    """Archive through ``node_retention_cleanup``'s own verified-copy code.

    The archive root is the directory the file-retention contract's ``archive_root_env`` names; a
    source is copied there as ``.gz`` or ``.tar.gz`` and read back by checksum before the pass may
    remove it, and ``synced`` reads the contract's ``synced_marker_xattr`` off the copy.
    """

    def root(self) -> Path | None:
        raw = os.environ.get(load_file_policy().archive_root_env)
        if not raw:
            return None
        path = Path(raw)
        return path if path.is_dir() else None

    def archive(self, source: Path, dest: Path) -> int:
        return _ensure_archive(source, dest)

    def verify(self, source: Path, dest: Path) -> None:
        _verify_archive(source, dest)

    def archivable(self, source: Path) -> bool:
        """The archive code copies regular files and leaves out symlinks and its own protected names (.git, memory,
        the ledger); an entry holding any of them would lose them at removal, so it is not archivable."""
        if source.is_symlink() or _is_protected(source):
            return False
        if not source.is_dir():
            return True
        for current, dirs, files in os.walk(source, followlinks=False):
            for name in (*dirs, *files):
                if _is_protected(Path(name)) or os.path.islink(
                    os.path.join(current, name)
                ):
                    return False
        return True

    def synced(self, dest: Path) -> bool:
        marker = load_file_policy().synced_marker_xattr
        return not marker or _has_marker(dest, marker)


@dataclass(frozen=True)
class HostRetentionPorts:
    """The twelve ports a handler needs, with a ``default()`` of the real adapters."""

    runner_state: CiHostStatePort
    runner_remove: CiHostRemovePort
    uv_cache: UvCachePort
    uv_archive: UvArchivePort
    lane_liveness: LaneLivenessPort
    container_mounts: ContainerMountPort
    unit_files: UnitFilesPort
    disk: DiskPort
    pre_commit_store: PreCommitStorePort
    lane_closure: LaneClosurePort = field(default_factory=LoaderLaneClosure)
    lane_receipts: LaneReceiptsPort = field(default_factory=FilesystemLaneReceipts)
    archive: ArchivePort = field(default_factory=RetentionCleanupArchive)

    @classmethod
    def default(cls, docker_timeout_s: float) -> HostRetentionPorts:
        return cls(
            runner_state=DockerCiHostState(docker_timeout_s),
            runner_remove=DockerCiHostRemove(docker_timeout_s),
            uv_cache=ProcessUvCache(),
            uv_archive=FilesystemUvArchive(),
            lane_liveness=FilesystemLaneLiveness(),
            container_mounts=DockerContainerMounts(docker_timeout_s),
            unit_files=FilesystemUnitFiles(),
            disk=ShutilDisk(),
            pre_commit_store=SqlitePreCommitStore(),
            lane_closure=LoaderLaneClosure(),
            lane_receipts=FilesystemLaneReceipts(),
            archive=RetentionCleanupArchive(),
        )
