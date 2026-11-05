# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Age out the entries directly under a host's declared temp directories, and bound its package stores.

The rule is the one the host applies to its run directories, applied to the entries those directories
skip: an entry that sits directly under a declared temp directory is removed when

* its owner is the user running the pass,
* no live process holds it (a process whose working directory is inside it, or whose command line,
  environment or open files name it),
* nothing under it was modified inside the retention window (the long window, or the short one once
  the filesystem holding the directory is past the pressure mark), and
* it holds no socket, pipe or other filesystem, because a live daemon may be listening there.

A directory is removed whole and a symlink is removed itself, never its target. A package store is
emptied, oldest version directory first, only when its filesystem is past the pressure mark, it is
over its declared ceiling and no live process names it: the store's index refers to the files in
it, so a partial eviction would leave an index row without a file.

Without a readable ``/proc`` nothing is removed, because a held entry cannot be ruled out.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import stat
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.nodes.node_temp_retention_effect.models.model_temp_retention import (
    ModelRetentionBounds,
    ModelRetentionPass,
    ModelRetentionSchedule,
    ModelStoreCeiling,
    ModelTempRetentionHost,
    ModelTempRetentionOverlay,
    ModelTempRoot,
)
from omnimarket.nodes.node_temp_retention_effect.models.model_temp_retention_request import (
    ModelTempRetentionRequest,
)

NODE_NAME = "node_temp_retention_effect"
NODE_DIR = Path(__file__).resolve().parents[1]
CONTRACT_PATH = NODE_DIR / "contract.yaml"
OVERLAY_ENV = "ONEX_TEMP_RETENTION_OVERLAY"
MIB = 1024 * 1024
LIST_CAP = 50
# A name inside a path, as a process names it: up to the next separator, quote or list mark.
NAME_AFTER_ROOT = r"/([^/\n\"':,;]+)"
# A command line that runs the tool owning a store: its processes may be writing the store.
PACKAGE_TOOL = {"pnpm": re.compile(r"(?<![\w.-])pnpm(?![\w-])")}


class RefusalError(Exception):
    """The overlay or contract declares something unsafe; nothing was read or removed."""


@dataclass(frozen=True)
class ProcessReferences:
    """What the live processes hold: their working directories, one text per process (command line,
    environment, working directory and open files) and each command line alone."""

    directories: list[Path] = field(default_factory=list)
    blobs: list[str] = field(default_factory=list)
    commands: list[str] = field(default_factory=list)


def read_process_references() -> ProcessReferences | None:
    """The working directory of every readable process and its command line, environment and fd links.

    None when ``/proc`` is not readable. A process of another user that cannot be read contributes
    nothing, and the owner check keeps that user's entries out of reach.
    """
    proc = Path("/proc")
    if not (proc / "self").is_dir():
        return None
    found = ProcessReferences()
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        parts: list[str] = []
        for name in ("cmdline", "environ"):
            with contextlib.suppress(OSError):
                text = (
                    (entry / name)
                    .read_bytes()
                    .replace(b"\0", b"\n")
                    .decode(errors="replace")
                )
                parts.append(text)
                if name == "cmdline":
                    found.commands.append(text.replace("\n", " "))
        with contextlib.suppress(OSError):
            cwd = os.readlink(entry / "cwd")
            found.directories.append(Path(cwd))
            parts.append(cwd)
        with contextlib.suppress(OSError):
            for descriptor in (entry / "fd").iterdir():
                with contextlib.suppress(OSError):
                    parts.append(os.readlink(descriptor))
        found.blobs.append("\n".join(parts))
    return found


def used_percent(path: Path) -> float:
    usage = shutil.disk_usage(path)
    return 100.0 * usage.used / usage.total if usage.total else 0.0


def load_contract(contract_path: Path = CONTRACT_PATH) -> dict[str, Any]:
    document = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise RefusalError(f"{contract_path}: the contract must be a mapping")
    return document


def load_bounds(contract_path: Path = CONTRACT_PATH) -> ModelRetentionBounds:
    return ModelRetentionBounds.model_validate(
        load_contract(contract_path)["retention_bounds"]
    )


def load_schedule(contract_path: Path = CONTRACT_PATH) -> ModelRetentionSchedule:
    return ModelRetentionSchedule.model_validate(
        load_contract(contract_path)["schedule"]
    )


def load_overlay(overlay_file: str = "") -> ModelTempRetentionOverlay:
    """Read the deployment overlay: the pinned file, else the environment pin, else the overlay roots."""
    if overlay_file:
        try:
            document = yaml.safe_load(Path(overlay_file).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, yaml.YAMLError) as exc:
            raise RefusalError(
                f"overlay {overlay_file} is unreadable ({type(exc).__name__})"
            ) from None
        return load_structured_overlay(
            NODE_NAME, ModelTempRetentionOverlay, explicit=document
        )
    return load_structured_overlay(
        NODE_NAME, ModelTempRetentionOverlay, file_env=OVERLAY_ENV
    )


def host_row(
    overlay: ModelTempRetentionOverlay, host_name: str
) -> ModelTempRetentionHost:
    names = [row.name for row in overlay.hosts]
    if len(set(names)) != len(names):
        raise RefusalError("a host is declared more than once in the overlay")
    for row in overlay.hosts:
        if row.name == host_name:
            return row
    raise RefusalError(
        f"{host_name}: not in the {NODE_NAME} overlay, so no retention is declared for it"
    )


# ---------------------------------------------------------------------------------------- reading


def _check_declared_path(path: str, leaf_names: tuple[str, ...], what: str) -> Path:
    declared = Path(path)
    if not declared.is_absolute() or os.path.normpath(path) != path or path == "/":
        raise RefusalError(f"{what} {path!r} must be an absolute, normalised path")
    if declared.name not in leaf_names:
        raise RefusalError(
            f"{what} {path!r} is named {declared.name!r}; the contract allows only "
            f"{', '.join(leaf_names)}"
        )
    if declared.is_symlink():
        raise RefusalError(f"{what} {path!r} is a symlink")
    return declared


@dataclass
class _Scan:
    newest: float
    special: bool


def _scan(path: Path, root_device: int, stop_after: float) -> _Scan:
    """The newest modification time anywhere under ``path`` (symlinks not followed), read until it passes
    ``stop_after``; ``special`` when a socket, pipe, device or other filesystem is inside."""
    info = path.lstat()
    newest = info.st_mtime
    special = info.st_dev != root_device or _is_special(info.st_mode)
    stack = [path] if stat.S_ISDIR(info.st_mode) else []
    while stack and newest < stop_after:
        directory = stack.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    try:
                        child = entry.stat(follow_symlinks=False)
                    except OSError:
                        newest = float(
                            "inf"
                        )  # an unreadable child counts as just touched
                        continue
                    newest = max(newest, child.st_mtime)
                    if child.st_dev != root_device or _is_special(child.st_mode):
                        special = True
                    if stat.S_ISDIR(child.st_mode):
                        stack.append(Path(entry.path))
        except OSError:
            newest = float("inf")
    return _Scan(newest=newest, special=special)


def _is_special(mode: int) -> bool:
    return (
        stat.S_ISSOCK(mode)
        or stat.S_ISFIFO(mode)
        or stat.S_ISBLK(mode)
        or stat.S_ISCHR(mode)
    )


def tree_bytes(path: Path) -> int:
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
        if stat.S_ISDIR(info.st_mode):
            with contextlib.suppress(OSError):
                stack.extend(current.iterdir())
    return total


def _held_names(root: Path, refs: ProcessReferences) -> set[str]:
    """The first path component under ``root`` that any live process names."""
    names: set[str] = set()
    for directory in refs.directories:
        with contextlib.suppress(ValueError):
            relative = directory.relative_to(root)
            if relative.parts:
                names.add(relative.parts[0])
    pattern = re.compile(re.escape(str(root)) + NAME_AFTER_ROOT)
    for blob in refs.blobs:
        for token in pattern.findall(blob):
            names.add(token)
            names.add(token.split()[0] if token.split() else token)
    return names


def _remove(path: Path) -> None:
    if path.is_symlink() or not path.is_dir():
        path.unlink()
    else:
        shutil.rmtree(path)


# ------------------------------------------------------------------------------------------ passes


def _pass_root(
    declared: ModelTempRoot,
    row: ModelTempRetentionHost,
    bounds: ModelRetentionBounds,
    *,
    now: float,
    uid: int,
    dry_run: bool,
    refs: ProcessReferences,
    used: Callable[[Path], float],
    out: dict[str, Any],
) -> None:
    root = _check_declared_path(
        declared.path, bounds.temp_root_leaf_names, "temp directory"
    )
    if not root.is_dir():
        out["refused"].append(f"{root}: not a directory, so nothing was read")
        return
    pressed = used(root) >= row.windows.pressure_percent
    hours = row.windows.pressure_retain_hours if pressed else row.windows.retain_hours
    out["windows"][str(root)] = hours
    cutoff = now - hours * 3600
    protected = [re.compile(pattern) for pattern in declared.protected_name_patterns]
    held = _held_names(root, refs)
    device = root.lstat().st_dev
    for entry in sorted(root.iterdir(), key=lambda item: item.name):
        try:
            info = entry.lstat()
        except OSError:
            continue
        if info.st_uid != uid or any(p.fullmatch(entry.name) for p in protected):
            out["kept"] += 1
            continue
        if entry.name in held:
            out["busy"].append(str(entry))
            continue
        scan = _scan(entry, device, cutoff)
        if scan.special or scan.newest >= cutoff:
            out["kept"] += 1
            continue
        size = tree_bytes(entry)
        if not dry_run:
            try:
                _remove(entry)
            except OSError as exc:
                out["errors"].append(f"{entry}: {type(exc).__name__}"[:300])
                if os.path.lexists(entry):
                    continue
        out["removed"].append(str(entry))
        out["freed"] += size


def _pass_store(
    ceiling: ModelStoreCeiling,
    row: ModelTempRetentionHost,
    bounds: ModelRetentionBounds,
    *,
    dry_run: bool,
    refs: ProcessReferences,
    used: Callable[[Path], float],
    out: dict[str, Any],
) -> None:
    store = _check_declared_path(
        ceiling.path, (bounds.store_leaf_names[ceiling.kind],), "package store"
    )
    if not store.is_dir():
        return
    if used(store) < row.windows.pressure_percent:
        return
    before = tree_bytes(store)
    limit = ceiling.ceiling_mib * MIB
    if before <= limit:
        return
    if store.name in _held_names(store.parent, refs) or any(
        PACKAGE_TOOL[ceiling.kind].search(command) for command in refs.commands
    ):
        out["stores"].append(
            f"{store}: held by a live {ceiling.kind} process, kept at "
            f"{before // MIB} MiB of {ceiling.ceiling_mib}"
        )
        return
    versions = sorted(
        (item for item in store.iterdir() if item.is_dir() and not item.is_symlink()),
        key=lambda item: item.lstat().st_mtime,
    )
    remaining = before
    removed = 0
    for version in versions:
        if remaining <= limit:
            break
        size = tree_bytes(version)
        if not dry_run:
            shutil.rmtree(version, ignore_errors=True)
        remaining -= size
        removed += 1
    out["freed"] += before - remaining
    out["stores"].append(
        f"{store}: {'would remove' if dry_run else 'removed'} {removed} version directories, "
        f"{(before - remaining) // MIB} MiB freed, now {remaining // MIB} MiB of {ceiling.ceiling_mib}"
    )


def run_pass(
    row: ModelTempRetentionHost,
    bounds: ModelRetentionBounds,
    *,
    now: float,
    uid: int,
    dry_run: bool = False,
    references: Callable[[], ProcessReferences | None] = read_process_references,
    used_percent: Callable[[Path], float] = used_percent,
    list_cap: int = LIST_CAP,
) -> ModelRetentionPass:
    """One pass over one host's declared temp directories and stores."""
    out: dict[str, Any] = {
        "removed": [],
        "busy": [],
        "refused": [],
        "errors": [],
        "stores": [],
        "windows": {},
        "kept": 0,
        "freed": 0,
    }
    refs = references()
    if refs is None:
        out["errors"].append(
            "process references unreadable (no /proc); nothing removed"
        )
    else:
        for declared in row.temp_roots:
            _pass_root(
                declared,
                row,
                bounds,
                now=now,
                uid=uid,
                dry_run=dry_run,
                refs=refs,
                used=used_percent,
                out=out,
            )
        for ceiling in row.store_ceilings:
            _pass_store(
                ceiling,
                row,
                bounds,
                dry_run=dry_run,
                refs=refs,
                used=used_percent,
                out=out,
            )
    return ModelRetentionPass(
        host=row.name,
        dry_run=dry_run,
        removed=tuple(out["removed"][:list_cap]),
        removed_count=len(out["removed"]),
        freed_mib=out["freed"] // MIB,
        busy=tuple(out["busy"][:list_cap]),
        busy_count=len(out["busy"]),
        kept_count=out["kept"],
        refused=tuple(out["refused"]),
        windows=out["windows"],
        stores=tuple(out["stores"]),
        errors=tuple(out["errors"]),
    )


class HandlerTempRetention:
    """``handle(request) -> result``: read the overlay row of the host and run one pass."""

    def __init__(
        self,
        *,
        now: Callable[[], float] | None = None,
        references: Callable[[], ProcessReferences | None] = read_process_references,
    ) -> None:
        import time

        self.now = now or time.time
        self.references = references

    def handle(self, request: ModelTempRetentionRequest) -> ModelRetentionPass:
        row = host_row(load_overlay(request.overlay_file), request.host_name)
        return run_pass(
            row,
            load_bounds(),
            now=self.now(),
            uid=os.getuid(),
            dry_run=request.dry_run,
            references=self.references,
        )
