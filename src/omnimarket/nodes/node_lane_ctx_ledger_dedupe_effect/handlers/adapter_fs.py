# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The host filesystem the dedupe runs against, and the in-memory one it defaults to in tests."""

from __future__ import annotations

import filecmp
import os
import stat
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from ..protocols import FileIdentity

CONTEXT_SUFFIX = ".ctx"  # the remote-lane runner's context directory: <run>.ctx
ARCHIVE_PARTS = ("ledger", "archive")  # its read-only ledger snapshot's archive rolls
ROLL_SUFFIX = ".md"


class HostLaneCtxFs:
    """The real backend: hard links on the host's own filesystem."""

    def context_dirs(self, runs_root: str) -> list[str]:
        found = []
        for entry in sorted(os.scandir(runs_root), key=lambda e: e.name):
            if entry.name.endswith(CONTEXT_SUFFIX) and entry.is_dir(
                follow_symlinks=False
            ):
                found.append(entry.path)
        return found

    def rolls(self, context_dir: str) -> list[str]:
        archive = os.path.join(context_dir, *ARCHIVE_PARTS)
        try:
            entries = sorted(os.scandir(archive), key=lambda e: e.name)
        except (FileNotFoundError, NotADirectoryError):
            return []
        return [
            e.path
            for e in entries
            if e.name.endswith(ROLL_SUFFIX) and e.is_file(follow_symlinks=False)
        ]

    def identity(self, path: str) -> FileIdentity | None:
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(st.st_mode):
            return None
        return FileIdentity(st.st_dev, st.st_ino, st.st_size, st.st_nlink, st.st_mtime)

    def same_bytes(self, left: str, right: str) -> bool:
        return filecmp.cmp(left, right, shallow=False)

    def ensure_dir(self, path: str) -> None:
        os.makedirs(path, exist_ok=True)

    def link_new(self, source: str, target: str) -> None:
        os.link(source, target, follow_symlinks=False)

    def replace_with_link(self, shared: str, roll: str) -> None:
        temp = os.path.join(
            os.path.dirname(roll), f".dedupe-{os.getpid()}-{os.path.basename(roll)}"
        )
        os.link(shared, temp, follow_symlinks=False)
        try:
            os.replace(temp, roll)
        except OSError:
            os.unlink(temp)
            raise


@dataclass
class _Inode:
    number: int
    data: bytes
    mtime: float
    links: int = 0


@dataclass
class MemoryLaneCtxFs:
    """The in-memory backend: paths name inodes, so a hard link is two paths on one inode."""

    names: dict[str, _Inode] = field(default_factory=dict)
    dirs: set[str] = field(default_factory=set)
    _next: int = 1
    fail_link_new: set[str] = field(default_factory=set)

    def add_roll(self, path: str, data: bytes, mtime: float) -> None:
        inode = _Inode(self._next, data, mtime, 1)
        self._next += 1
        self.names[path] = inode
        self.dirs.add(str(PurePosixPath(path).parent))

    def context_dirs(self, runs_root: str) -> list[str]:
        prefix = runs_root.rstrip("/") + "/"
        found = {
            prefix + path[len(prefix) :].split("/", 1)[0]
            for path in self.names
            if path.startswith(prefix)
        }
        return sorted(p for p in found if p.endswith(CONTEXT_SUFFIX))

    def rolls(self, context_dir: str) -> list[str]:
        archive = "/".join([context_dir.rstrip("/"), *ARCHIVE_PARTS]) + "/"
        return sorted(
            p for p in self.names if p.startswith(archive) and p.endswith(ROLL_SUFFIX)
        )

    def identity(self, path: str) -> FileIdentity | None:
        inode = self.names.get(path)
        if inode is None:
            return None
        return FileIdentity(1, inode.number, len(inode.data), inode.links, inode.mtime)

    def same_bytes(self, left: str, right: str) -> bool:
        return self.names[left].data == self.names[right].data

    def ensure_dir(self, path: str) -> None:
        self.dirs.add(path)

    def link_new(self, source: str, target: str) -> None:
        if source in self.fail_link_new:
            raise OSError(18, "Invalid cross-device link")
        inode = self.names[source]
        inode.links += 1
        self.names[target] = inode

    def replace_with_link(self, shared: str, roll: str) -> None:
        self.names[roll].links -= 1
        inode = self.names[shared]
        inode.links += 1
        self.names[roll] = inode
