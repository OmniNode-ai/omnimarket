# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read the consumers of a host's disk by class (OMN-23865).

One ``ModelDiskClassReading`` per class in ``ALL_CLASSES``: the filesystem classes from the declared
roots and the docker classes from the two docker commands. A class with no declared root, or a root
that cannot be read, is reported unread with a reason, never a guessed size.
"""

from __future__ import annotations

import json
import os
import pwd
import re
import stat
import subprocess
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import NamedTuple

from omnimarket.nodes.node_lab_disk_watermark_effect.models.model_host_disk_reading import (
    ALL_CLASSES,
    ModelDiskClassReading,
    ModelDiskGroup,
)
from omnimarket.nodes.node_lab_disk_watermark_effect.models.model_lab_disk_watermark import (
    ModelHostDiskReadingSettings,
    ModelWatermarkHost,
)
from omnimarket.nodes.node_temp_retention_effect.handlers.handler_temp_retention import (
    ProcessReferences,
    _held_names,
    read_process_references,
)

_SIZE_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*([BKMGT])B?\b", re.IGNORECASE)
_UNIT = {"B": 1, "K": 1000, "M": 1000**2, "G": 1000**3, "T": 1000**4}


class _Walk(NamedTuple):
    size: int
    newest: float


def walk_entry(path: Path) -> _Walk:
    """Allocated bytes (each inode once, symlinks not followed, as ``du -s``) and the newest change under ``path``, in one walk.

    node_temp_retention_effect takes the two in two walks (``tree_bytes`` and ``_scan``); a reading walks a cache of
    thousands of entries, so one walk halves the stat calls. An entry or child that cannot be read counts as just touched.
    """
    size = 0
    newest = 0.0
    seen: set[tuple[int, int]] = set()
    stack = [str(path)]
    while stack:
        current = stack.pop()
        try:
            info = os.lstat(current)
        except OSError:
            newest = float("inf")
            continue
        newest = max(newest, info.st_mtime)
        if info.st_nlink > 1 and not stat.S_ISDIR(info.st_mode):
            key = (info.st_dev, info.st_ino)
            if key in seen:
                continue
            seen.add(key)
        size += info.st_blocks * 512
        if stat.S_ISDIR(info.st_mode):
            try:
                with os.scandir(current) as children:
                    stack.extend(child.path for child in children)
            except OSError:
                newest = float("inf")
    return _Walk(size, newest)


class _Item(NamedTuple):
    size: int
    age: int
    owner: str
    held: bool


def volume_for(path: str, volume_paths: Sequence[str]) -> str | None:
    """The longest volume path that is a path-component prefix of ``path``, else None."""
    parts = [part for part in path.split("/") if part]
    best: str | None = None
    for volume in volume_paths:
        vparts = [part for part in volume.split("/") if part]
        is_prefix = len(vparts) <= len(parts) and parts[: len(vparts)] == vparts
        if is_prefix and (best is None or len(volume) > len(best)):
            best = volume
    return best


def parse_docker_size(text: str) -> int:
    """Docker's 1000-based sizes: ``4.5GB`` is 4500000000; ``0B (virtual 1.2GB)`` is 0."""
    match = _SIZE_RE.match(text)
    if match is None:
        raise ValueError(f"not a docker size: {text!r}")
    return int(float(match.group(1)) * _UNIT[match.group(2).upper()])


def _default_run_docker(command: Sequence[str], timeout: int) -> str:
    result = subprocess.run(
        list(command), capture_output=True, text=True, timeout=timeout, check=False
    )
    if result.returncode != 0:
        raise ValueError(f"docker command exited {result.returncode}")
    return result.stdout


def _dist_info_key(entry: Path) -> tuple[str, str]:
    """The (package, revision) of a uv-cache entry from its ``*.dist-info`` directory, at most two levels down."""
    level = [entry]
    for _ in range(2):
        below: list[Path] = []
        for directory in level:
            try:
                children = [
                    child
                    for child in directory.iterdir()
                    if child.is_dir() and not child.is_symlink()
                ]
            except OSError:
                continue
            for child in children:
                if child.name.endswith(".dist-info") and "-" in child.name:
                    package, version = child.name[: -len(".dist-info")].rsplit("-", 1)
                    return (package.lower().replace("_", "-"), version)
            below.extend(children)
        level = below
    return ("unclassified", "")


def _owner_name(path: Path) -> str:
    try:
        return pwd.getpwuid(path.lstat().st_uid).pw_name
    except (KeyError, OSError):
        return str(path.lstat().st_uid)


class HostDiskClassReader:
    """Read the consumers of a host's disk by class; ``now``, ``process_references`` and ``run_docker`` are injected."""

    def __init__(
        self,
        *,
        now: Callable[[], float] = time.time,
        process_references: Callable[
            [], ProcessReferences | None
        ] = read_process_references,
        run_docker: Callable[[Sequence[str], int], str] | None = None,
    ) -> None:
        self.now = now
        self.process_references = process_references
        self.run_docker = run_docker if run_docker is not None else _default_run_docker
        self._docker_output: dict[tuple[str, ...], str] = {}

    def _docker_text(self, command: Sequence[str], timeout: int) -> str:
        """One docker command is run once per reading, however many classes read its output."""
        key = tuple(command)
        if key not in self._docker_output:
            self._docker_output[key] = self.run_docker(command, timeout)
        return self._docker_output[key]

    def read_classes(
        self, row: ModelWatermarkHost, settings: ModelHostDiskReadingSettings
    ) -> tuple[ModelDiskClassReading, ...]:
        refs = self.process_references()
        self._docker_output = {}
        return tuple(self._class(name, row, settings, refs) for name in ALL_CLASSES)

    def _class(
        self,
        name: str,
        row: ModelWatermarkHost,
        settings: ModelHostDiskReadingSettings,
        refs: ProcessReferences | None,
    ) -> ModelDiskClassReading:
        if name in ("uv_cache", "worktrees", "lab_run"):
            return self._filesystem(name, row, settings, refs)
        return self._docker(name, row, settings, refs)

    def _filesystem(
        self,
        name: str,
        row: ModelWatermarkHost,
        settings: ModelHostDiskReadingSettings,
        refs: ProcessReferences | None,
    ) -> ModelDiskClassReading:
        root = next((r for r in row.class_roots if r.name == name), None)
        if root is None:
            return ModelDiskClassReading(
                class_name=name,
                status="unread",
                unread_reason="class root not declared in the overlay",
            )
        entries_dir = (
            Path(root.path) / root.entries_subdir
            if root.entries_subdir
            else Path(root.path)
        )
        try:
            entries = [
                child
                for child in entries_dir.iterdir()
                if child.is_dir() and not child.is_symlink()
            ]
        except OSError as exc:
            return ModelDiskClassReading(
                class_name=name,
                status="unread",
                unread_reason=f"root {root.path} unreadable ({type(exc).__name__})",
            )
        held = _held_names(entries_dir, refs) if refs is not None else set()
        groups: dict[tuple[str, str], list[_Item]] = {}
        total = 0
        for child in entries:
            walked = walk_entry(child)
            size = walked.size
            age = (
                0
                if walked.newest == float("inf")
                else max(0, int(self.now() - walked.newest))
            )
            key = _dist_info_key(child) if name == "uv_cache" else (child.name, "")
            groups.setdefault(key, []).append(
                _Item(size, age, _owner_name(child), child.name in held)
            )
            total += size
        built = [
            ModelDiskGroup(
                package=key[0],
                revision=key[1],
                copies=len(items),
                bytes=sum(item.size for item in items),
                newest_use_age_seconds=min(item.age for item in items),
                oldest_use_age_seconds=max(item.age for item in items),
                held_copies=sum(1 for item in items if item.held),
                owners=tuple(sorted({item.owner for item in items})),
            )
            for key, items in groups.items()
        ]
        built.sort(key=lambda group: (-group.bytes, group.package, group.revision))
        kept, omitted = (
            built[: settings.max_groups_per_class],
            len(built) - settings.max_groups_per_class,
        )
        return ModelDiskClassReading(
            class_name=name,
            status="read",
            total_bytes=total,
            entry_count=len(entries),
            revision_count=len(groups),
            groups=tuple(kept),
            groups_omitted=max(0, omitted),
            unreadable_process_count=len(refs.unreadable) if refs is not None else 0,
        )

    def _docker(
        self,
        name: str,
        row: ModelWatermarkHost,
        settings: ModelHostDiskReadingSettings,
        refs: ProcessReferences | None,
    ) -> ModelDiskClassReading:
        if row.docker is None:
            return ModelDiskClassReading(
                class_name=name,
                status="unread",
                unread_reason="docker reader not declared in the overlay",
            )
        try:
            df = [
                json.loads(line)
                for line in self._docker_text(
                    row.docker.df_command, row.docker.timeout_seconds
                ).splitlines()
                if line.strip()
            ]
            if name == "docker_exited_containers":
                lines = [
                    json.loads(line)
                    for line in self._docker_text(
                        row.docker.exited_command, row.docker.timeout_seconds
                    ).splitlines()
                    if line.strip()
                ]
                groups: dict[tuple[str, str], list[int]] = {}
                for line in lines:
                    groups.setdefault((line["Names"], line["Image"]), []).append(
                        parse_docker_size(line["Size"])
                    )
                built = [
                    ModelDiskGroup(
                        package=key[0],
                        revision=key[1],
                        copies=len(sizes),
                        bytes=sum(sizes),
                        newest_use_age_seconds=None,
                        oldest_use_age_seconds=None,
                        held_copies=0,
                        owners=(),
                    )
                    for key, sizes in groups.items()
                ]
                built.sort(
                    key=lambda group: (-group.bytes, group.package, group.revision)
                )
                kept, omitted = (
                    built[: settings.max_groups_per_class],
                    len(built) - settings.max_groups_per_class,
                )
                return ModelDiskClassReading(
                    class_name=name,
                    status="read",
                    total_bytes=sum(sum(sizes) for sizes in groups.values()),
                    entry_count=len(lines),
                    revision_count=len(groups),
                    groups=tuple(kept),
                    groups_omitted=max(0, omitted),
                    unreadable_process_count=len(refs.unreadable)
                    if refs is not None
                    else 0,
                )
            row_type = {
                "docker_images": "Images",
                "docker_volumes": "Local Volumes",
                "docker_build_cache": "Build Cache",
            }[name]
            line = next((item for item in df if item["Type"] == row_type), None)
            if line is None:
                return ModelDiskClassReading(
                    class_name=name,
                    status="unread",
                    unread_reason="docker df printed no row for it",
                )
            group = ModelDiskGroup(
                package=row_type.lower().replace(" ", "_"),
                revision="",
                copies=max(int(line["TotalCount"]), 1),
                bytes=parse_docker_size(line["Size"]),
                newest_use_age_seconds=None,
                oldest_use_age_seconds=None,
                held_copies=0,
                owners=(),
            )
            return ModelDiskClassReading(
                class_name=name,
                status="read",
                total_bytes=group.bytes,
                entry_count=int(line["TotalCount"]),
                revision_count=1,
                groups=(group,),
                unreadable_process_count=len(refs.unreadable)
                if refs is not None
                else 0,
            )
        except (
            ValueError,
            OSError,
            subprocess.TimeoutExpired,
            json.JSONDecodeError,
            KeyError,
            TypeError,
        ) as exc:
            return ModelDiskClassReading(
                class_name=name,
                status="unread",
                unread_reason=f"docker read failed ({type(exc).__name__})",
            )


__all__: list[str] = ["HostDiskClassReader", "parse_docker_size", "volume_for"]
