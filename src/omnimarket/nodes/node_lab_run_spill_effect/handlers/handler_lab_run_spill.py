# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Sparse-aware, byte-capped spill of idle lab run state (moved from the host installer, OMN-23384).

A file is copied by its data extents only (``SEEK_DATA``/``SEEK_HOLE``), so a hole in the source is a hole in the
copy. A directory or file whose name is a declared skip name or matches a declared pattern is never entered. The
bytes the spill root holds are counted as allocated blocks, and a spill stops when the next file would pass the
cap the host's overlay row declares.
"""

from __future__ import annotations

import contextlib
import errno
import fnmatch
import os
import shutil
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.nodes.node_lab_run_spill_effect.models.model_lab_run_spill import (
    ModelLabRunSpillOverlay,
    ModelLabRunSpillRequest,
    ModelLabRunSpillResult,
    ModelSpillHostRow,
)

NODE_NAME = "node_lab_run_spill_effect"
OVERLAY_ENV = "ONEX_LAB_RUN_SPILL_OVERLAY"
CHUNK = 1024 * 1024
MIB = 1024 * 1024

Copier = Callable[[Path, Path], None]


class SpillRefusalError(ValueError):
    """The overlay lacks the host, or a request names something the node will not act on."""


@dataclass
class _Tally:
    """Running totals of one spill; ``capped`` is set when the next file would pass ``cap``."""

    held: int
    cap: int
    copied: int = 0
    allocated: int = 0
    logical: int = 0
    capped: bool = False
    skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def copy_sparse(origin: Path, target: Path) -> None:
    """Copy ``origin`` to ``target`` keeping every hole; mode and times follow as ``copy2`` does."""
    with origin.open("rb") as source, target.open("wb") as destination:
        size = os.fstat(source.fileno()).st_size
        destination.truncate(size)
        read_fd, write_fd = source.fileno(), destination.fileno()
        offset = 0
        while offset < size:
            try:
                start = os.lseek(read_fd, offset, os.SEEK_DATA)
            except OSError as exc:
                if exc.errno == errno.ENXIO:
                    break
                if exc.errno == errno.EINVAL:
                    shutil.copyfileobj(source, destination)
                    break
                raise
            end = os.lseek(read_fd, start, os.SEEK_HOLE)
            position = start
            while position < end:
                block = os.pread(read_fd, min(CHUNK, end - position), position)
                if not block:
                    break
                os.pwrite(write_fd, block, position)
                position += len(block)
            offset = end
    shutil.copystat(origin, target)


def allocated_bytes(path: Path) -> int:
    """Allocated bytes under ``path``, each inode once, symlinks not followed (as ``du -s``)."""
    seen: set[tuple[int, int]] = set()
    total = 0
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            info = current.lstat()
        except OSError:
            continue
        key = (info.st_dev, info.st_ino)
        if key not in seen:
            seen.add(key)
            total += info.st_blocks * 512
        if current.is_dir() and not current.is_symlink():
            with contextlib.suppress(OSError):
                stack.extend(current.iterdir())
    return total


def newest_mtime(path: Path) -> float:
    """The newest modification time anywhere under ``path`` (symlinks are not followed)."""
    newest = path.lstat().st_mtime
    stack = [path]
    while stack:
        directory = stack.pop()
        with contextlib.suppress(OSError), os.scandir(directory) as entries:
            for entry in entries:
                with contextlib.suppress(OSError):
                    newest = max(newest, entry.stat(follow_symlinks=False).st_mtime)
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(Path(entry.path))
    return newest


def load_overlay(overlay_file: str = "") -> ModelLabRunSpillOverlay:
    """Read the pinned file, else the environment pin, else the overlay roots."""
    if overlay_file:
        try:
            document = yaml.safe_load(Path(overlay_file).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, yaml.YAMLError) as exc:
            raise SpillRefusalError(
                f"overlay {overlay_file} is unreadable ({type(exc).__name__})"
            ) from None
        return load_structured_overlay(
            NODE_NAME, ModelLabRunSpillOverlay, explicit=document
        )
    return load_structured_overlay(
        NODE_NAME, ModelLabRunSpillOverlay, file_env=OVERLAY_ENV
    )


def host_row(overlay: ModelLabRunSpillOverlay, host_name: str) -> ModelSpillHostRow:
    rows = [row for row in overlay.hosts if row.name == host_name]
    if len(rows) != 1:
        raise SpillRefusalError(
            f"{host_name}: {len(rows)} rows in the {NODE_NAME} overlay, so no spill is declared for it"
        )
    return rows[0]


class HandlerLabRunSpill:
    """Run one spill action; ``copier`` and ``now`` are injected so a test names its own."""

    def __init__(
        self,
        *,
        copier: Copier = copy_sparse,
        now: Callable[[], float] = time.time,
    ) -> None:
        self.copier = copier
        self.now = now

    def handle(self, request: ModelLabRunSpillRequest) -> ModelLabRunSpillResult:
        overlay = load_overlay(request.overlay_file)
        row = host_row(overlay, request.host_name)
        spill_root = Path(request.spill_root)
        source = Path(request.source)
        cap = row.spill_byte_cap_mib * MIB
        if request.action == "prune":
            return self._prune(request, spill_root)
        if request.action == "preserve_worktree":
            preserved = (
                False if request.dry_run else self.preserve_worktree(source, spill_root)
            )
            return ModelLabRunSpillResult(
                action=request.action, host_name=request.host_name, preserved=preserved
            )
        if request.action == "restore":
            return self._restore(request, overlay, spill_root, source)
        return self._spill(request, overlay, row, spill_root, source, cap)

    def skips(self, row: ModelSpillHostRow, name: str) -> bool:
        return name in row.skip_names or any(
            fnmatch.fnmatchcase(name, pattern) for pattern in row.skip_patterns
        )

    def _spill(
        self,
        request: ModelLabRunSpillRequest,
        overlay: ModelLabRunSpillOverlay,
        row: ModelSpillHostRow,
        spill_root: Path,
        source: Path,
        cap: int,
    ) -> ModelLabRunSpillResult:
        spill_root.mkdir(parents=True, exist_ok=True)
        tally = _Tally(held=allocated_bytes(spill_root), cap=cap)
        if request.action == "spill_all":
            directories = [
                entry
                for entry in sorted(source.iterdir())
                if entry.is_dir()
                and not entry.is_symlink()
                and entry.name.endswith(overlay.state_suffix)
            ]
        else:
            directories = [source]
        for directory in directories:
            if tally.capped:
                break
            self._spill_tree(
                directory, spill_root / directory.name, row, tally, request.dry_run
            )
        return ModelLabRunSpillResult(
            action=request.action,
            host_name=request.host_name,
            files_copied=tally.copied,
            bytes_allocated=tally.allocated,
            bytes_logical=tally.logical,
            byte_cap=cap,
            spill_root_bytes=tally.held + tally.allocated,
            capped=tally.capped,
            skipped=tuple(tally.skipped),
            errors=tuple(tally.errors),
        )

    def _spill_tree(
        self,
        source: Path,
        destination: Path,
        row: ModelSpillHostRow,
        tally: _Tally,
        dry_run: bool,
    ) -> None:
        for directory, names, files in os.walk(source):
            kept = [name for name in names if not self.skips(row, name)]
            tally.skipped.extend(
                str(Path(directory) / name) for name in names if name not in kept
            )
            names[:] = kept
            relative = Path(directory).relative_to(source)
            if not dry_run:
                (destination / relative).mkdir(parents=True, exist_ok=True)
            for name in files:
                origin = Path(directory) / name
                if self.skips(row, name):
                    tally.skipped.append(str(origin))
                    continue
                target = destination / relative / name
                try:
                    info = origin.lstat()
                    if origin.is_symlink() or (
                        target.exists() and target.stat().st_mtime >= info.st_mtime
                    ):
                        continue
                    need = info.st_blocks * 512
                    if tally.held + tally.allocated + need > tally.cap:
                        tally.capped = True
                        return
                    if dry_run:
                        used = need
                    else:
                        self.copier(origin, target)
                        used = target.stat().st_blocks * 512
                    tally.copied += 1
                    tally.allocated += used
                    tally.logical += info.st_size
                except OSError as exc:
                    tally.errors.append(f"{origin}: {type(exc).__name__}")

    def _restore(
        self,
        request: ModelLabRunSpillRequest,
        overlay: ModelLabRunSpillOverlay,
        spill_root: Path,
        runs: Path,
    ) -> ModelLabRunSpillResult:
        restored = 0
        errors: list[str] = []
        if spill_root.is_dir():
            for entry in sorted(spill_root.iterdir()):
                if not entry.is_dir() or not entry.name.endswith(overlay.state_suffix):
                    continue
                target = runs / entry.name
                if target.exists():
                    continue
                if request.dry_run:
                    restored += 1
                    continue
                try:
                    shutil.copytree(
                        entry,
                        target,
                        symlinks=True,
                        copy_function=lambda origin, copy: self.copier(
                            Path(origin), Path(copy)
                        ),
                    )
                    restored += 1
                except (OSError, shutil.Error) as exc:
                    errors.append(f"{entry}: {type(exc).__name__}")
        return ModelLabRunSpillResult(
            action=request.action,
            host_name=request.host_name,
            restored=restored,
            errors=tuple(errors),
        )

    def _prune(
        self, request: ModelLabRunSpillRequest, spill_root: Path
    ) -> ModelLabRunSpillResult:
        removed: list[str] = []
        if spill_root.is_dir():
            horizon = request.keep_days * 86400
            now = self.now()
            for entry in sorted(spill_root.iterdir()):
                if now - newest_mtime(entry) >= horizon:
                    if not request.dry_run:
                        if entry.is_dir() and not entry.is_symlink():
                            shutil.rmtree(entry, ignore_errors=True)
                        else:
                            entry.unlink(missing_ok=True)
                    removed.append(entry.name)
        return ModelLabRunSpillResult(
            action=request.action,
            host_name=request.host_name,
            pruned=tuple(removed),
        )

    def preserve_worktree(self, worktree: Path, spill_root: Path) -> bool:
        """Write a worktree's uncommitted change to the spill root; True if there was any."""
        if not (worktree / ".git").exists():
            return False
        status = subprocess.run(
            ["git", "-C", str(worktree), "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=False,
        )
        if status.returncode or not status.stdout.strip():
            return False
        diff = subprocess.run(
            ["git", "-C", str(worktree), "diff", "HEAD", "--binary"],
            capture_output=True,
            check=False,
        )
        spill_root.mkdir(parents=True, exist_ok=True)
        (spill_root / f"{worktree.name}.dirty.patch").write_bytes(diff.stdout)
        (spill_root / f"{worktree.name}.dirty.status").write_text(
            status.stdout, encoding="utf-8"
        )
        return True
