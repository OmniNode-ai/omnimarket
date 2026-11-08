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
# A name inside a path, as a process names it: up to the next separator or quote. A colon, comma or
# semicolon is a legal character of a name, so the whole run is one name; the pieces either side of such
# a mark are held too, because the same text is also a path list.
NAME_AFTER_ROOT = r"/([^/\n\"']+)"
LIST_MARKS = re.compile(r"[:,;]")
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
    unreadable: list[int] = field(
        default_factory=list
    )  # same-user processes that refused a read


def read_process_references() -> ProcessReferences | None:
    """The working directory of every readable process and its command line, environment and fd links.

    None when ``/proc`` is not readable. A process of another user that cannot be read contributes
    nothing, and the owner check keeps that user's entries out of reach. A process of the running user
    that refuses a read is listed in ``unreadable``: it may hold an entry the pass cannot see, and a
    login manager or a session of the running user is commonly such a process, so the pass reports the
    count (``unreadable_count``) with its result and does not stop on it.
    """
    proc = Path("/proc")
    if not (proc / "self").is_dir():
        return None
    found = ProcessReferences()
    me = os.getuid()
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            same_user = entry.stat().st_uid == me
        except OSError:
            continue  # gone since it was listed
        refused = False
        parts: list[str] = []
        for name in ("cmdline", "environ"):
            try:
                text = (
                    (entry / name)
                    .read_bytes()
                    .replace(b"\0", b"\n")
                    .decode(errors="replace")
                )
                parts.append(text)
                if name == "cmdline":
                    found.commands.append(text.replace("\n", " "))
            except PermissionError:
                refused = True
            except OSError:
                pass
        try:
            cwd = os.readlink(entry / "cwd")
            found.directories.append(Path(cwd))
            parts.append(cwd)
        except PermissionError:
            refused = True
        except OSError:
            pass
        try:
            for descriptor in (entry / "fd").iterdir():
                with contextlib.suppress(OSError):
                    parts.append(os.readlink(descriptor))
        except PermissionError:
            refused = True
        except OSError:
            pass
        if refused and same_user:
            found.unreadable.append(int(entry.name))
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
        if not isinstance(document, dict):
            raise RefusalError(
                f"overlay {overlay_file} must be a mapping, so no other overlay source is tried"
            )
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
    if declared.is_symlink() or os.path.realpath(path) != path:
        raise RefusalError(f"{what} {path!r} is a symlink or sits under one")
    return declared


def validate_declarations(
    row: ModelTempRetentionHost, bounds: ModelRetentionBounds
) -> list[list[re.Pattern[str]]]:
    """Refuse every unsafe declaration of a host row before anything is read or removed.

    Returns, for each temp directory in declaration order, its compiled protected-name patterns.
    """
    patterns: list[list[re.Pattern[str]]] = []
    for declared in row.temp_roots:
        _check_declared_path(
            declared.path, bounds.temp_root_leaf_names, "temp directory"
        )
        compiled: list[re.Pattern[str]] = []
        for pattern in declared.protected_name_patterns:
            try:
                compiled.append(re.compile(pattern))
            except re.error as exc:
                raise RefusalError(
                    f"protected name pattern {pattern!r} does not compile ({exc})"
                ) from None
        patterns.append(compiled)
    for ceiling in row.store_ceilings:
        _check_declared_path(
            ceiling.path, (bounds.store_leaf_names[ceiling.kind],), "package store"
        )
    return patterns


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
            for candidate in (token, *LIST_MARKS.split(token)):
                names.add(candidate)
                names.add(candidate.split()[0] if candidate.split() else candidate)
    return names


def _make_owned_directories_writable(path: Path, uid: int) -> None:
    """Give the owner full access to every directory under ``path`` that ``uid`` owns.

    A tree a test left with a read-only directory cannot be emptied: the files inside it cannot be
    unlinked, and ``rmtree`` stops with ``PermissionError``. Only directories owned by the running
    user are changed, symlinks are never followed, and only the owner bits are added.
    """
    stack = [path]
    while stack:
        directory = stack.pop()
        try:
            # Open the directory itself (never a symlink) and change the inode that was checked.
            # O_PATH exists on Linux only (this path also uses /proc); mypy on macOS lacks it.
            flags = (
                getattr(os, "O_PATH", 0) | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
            )
            descriptor = os.open(directory, flags)
            try:
                info = os.fstat(descriptor)
                if not stat.S_ISDIR(info.st_mode) or info.st_uid != uid:
                    continue
                wanted = stat.S_IMODE(info.st_mode) | stat.S_IRWXU
                if wanted != stat.S_IMODE(info.st_mode):
                    os.chmod(f"/proc/self/fd/{descriptor}", wanted)
            finally:
                os.close(descriptor)
            stack.extend(
                Path(entry.path)
                for entry in os.scandir(directory)
                if entry.is_dir(follow_symlinks=False)
            )
        except OSError:
            continue


def _remove(path: Path, uid: int) -> None:
    if path.is_symlink() or not path.is_dir():
        path.unlink()
        return
    try:
        shutil.rmtree(path)
    except PermissionError:
        _make_owned_directories_writable(path, uid)
        shutil.rmtree(path)


# ------------------------------------------------------------------------------------------ passes


def _pass_root(
    declared: ModelTempRoot,
    protected: list[re.Pattern[str]],
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
                _remove(entry, uid)
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
    uid: int,
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
            try:
                _remove(version, uid)
            except OSError as exc:
                out["errors"].append(f"{version}: {type(exc).__name__}"[:300])
            if os.path.lexists(version):
                # A failed deletion frees only what it did remove, and is not counted as removed.
                remaining -= max(size - tree_bytes(version), 0)
                continue
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
        "unreadable": 0,
    }
    # Every declaration is checked before the first removal, so a refusal never follows a removal.
    protected = validate_declarations(row, bounds)
    # The references are read again before each directory and store: a process that starts during a
    # long scan of an earlier one is seen by the next.
    roots = list(zip(row.temp_roots, protected, strict=True))
    for index in range(len(roots) + len(row.store_ceilings)):
        refs = references()
        if refs is None:
            out["errors"].append(
                "process references unreadable (no /proc); nothing removed"
            )
            break
        out["unreadable"] = max(out["unreadable"], len(refs.unreadable))
        if index < len(roots):
            declared, patterns = roots[index]
            _pass_root(
                declared,
                patterns,
                row,
                bounds,
                now=now,
                uid=uid,
                dry_run=dry_run,
                refs=refs,
                used=used_percent,
                out=out,
            )
        else:
            _pass_store(
                row.store_ceilings[index - len(roots)],
                row,
                bounds,
                uid=uid,
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
        unreadable_count=out["unreadable"],
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
