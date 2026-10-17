# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""NodeFileRetention -- retires aged files named in the retention contract.

The sibling of the table pass in ``handler_retention_cleanup``: the same
contract (``retention_policy.file_retention``) declares each unbounded lab path
with the keep window its readers set and an action, so one timer per host runs
the tables and the files together (OMN-17427).

A *unit* is one file or one directory matched by an entry's ``pattern`` under
its ``path``. A unit is retired only when every file inside it is older than
``keep_days``; one recent file keeps the whole unit. ``keep_days`` has a floor
of seven days: nothing modified in the last week is ever touched.

Actions:

``delete``
    Remove the unit.
``archive``
    History an operator may want back. Compress the unit into
    ``<archive_root>/<host>/<entry name>/``, read the copy back and compare
    checksums with every source file, and only then remove the local unit.
    With no archive tree, or on any mismatch, nothing is removed.

Never touched: symlinks (not followed, not removed), anything named
``memory`` or ``MEMORY.md``, and the live ledger. Dry run lists what would be
retired and writes and removes nothing.
"""

from __future__ import annotations

import datetime as dt
import fnmatch
import gzip
import hashlib
import os
import shutil
import socket
import subprocess
import tarfile
import zlib
from collections.abc import Iterator
from pathlib import Path
from typing import IO, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

MIN_KEEP_DAYS = 7
_CONTRACT_PATH = Path(__file__).resolve().parent.parent / "contract.yaml"
_PROTECTED_NAMES: frozenset[str] = frozenset({"memory", "MEMORY.md"})
_PROTECTED_PREFIX = "ROLLING_WORK_LEDGER"
_CHUNK = 1 << 20


class FileRetentionEntry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    path: str
    pattern: str = "*"
    keep_days: int
    action: Literal["delete", "archive"]
    exclude: tuple[str, ...] = Field(default_factory=tuple)
    readers: tuple[str, ...] = Field(default_factory=tuple)
    enabled: bool = True
    consent_ref: str = ""

    @field_validator("keep_days")
    @classmethod
    def _floor(cls, value: int) -> int:
        if value < MIN_KEEP_DAYS:
            msg = f"keep_days {value} is under the {MIN_KEEP_DAYS}-day floor"
            raise ValueError(msg)
        return value


class FileRetentionPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    archive_root_env: str
    synced_marker_xattr: str = ""
    entries: list[FileRetentionEntry]


class FileRetentionRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    entries: list[FileRetentionEntry]
    now: dt.datetime
    dry_run: bool = False
    host: str = ""
    archive_root: str = ""
    synced_marker: str = Field(
        default="",
        description=(
            "Extended attribute the archive tree's sync client sets on a file once "
            "it has registered it; an archive copy without it keeps its local unit"
        ),
    )
    only: tuple[str, ...] = Field(
        default_factory=tuple, description="Entry names to run; empty runs all"
    )


class FileRetentionItemResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    status: Literal[
        "ok", "dry_run", "absent", "disabled", "archive_unavailable", "error"
    ]
    action: str = ""
    units_removed: int = 0
    files_removed: int = 0
    bytes_removed: int = 0
    files_archived: int = 0
    bytes_archived: int = 0
    bytes_before: int = 0
    bytes_after: int = 0
    units_pending_sync: int = 0
    bytes_pending_sync: int = 0
    error: str = ""
    note: str = ""


class FileRetentionResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str
    dry_run: bool
    items: list[FileRetentionItemResult] = Field(default_factory=list)
    files_removed: int = 0
    bytes_removed: int = 0
    files_archived: int = 0
    bytes_archived: int = 0


def load_file_policy(contract_path: Path = _CONTRACT_PATH) -> FileRetentionPolicy:
    raw = yaml.safe_load(contract_path.read_text()) or {}
    block = (raw.get("retention_policy") or {}).get("file_retention")
    if not block:
        msg = f"contract.yaml at {contract_path} declares no retention_policy.file_retention"
        raise RuntimeError(msg)
    return FileRetentionPolicy.model_validate(block)


def default_host() -> str:
    return os.environ.get("ONEX_RETENTION_HOST") or socket.gethostname().split(".")[0]


class _Unit:
    __slots__ = ("bytes", "files", "newest", "path")

    def __init__(self, path: Path) -> None:
        self.path = path
        self.files: list[Path] = []
        self.bytes = 0
        self.newest = 0.0


def _is_protected(path: Path) -> bool:
    return path.name in _PROTECTED_NAMES or path.name.startswith(_PROTECTED_PREFIX)


def _walk(path: Path) -> Iterator[Path]:
    """Regular files under ``path``, without following links or entering memory."""
    if path.is_symlink() or _is_protected(path):
        return
    if path.is_dir():
        for child in sorted(path.iterdir()):
            yield from _walk(child)
    elif path.is_file():
        yield path


def _measure(path: Path) -> _Unit:
    unit = _Unit(path)
    for file in _walk(path):
        st = file.lstat()
        unit.files.append(file)
        unit.bytes += st.st_size
        unit.newest = max(unit.newest, st.st_mtime)
    return unit


def _units(root: Path, entry: FileRetentionEntry) -> list[_Unit]:
    out: list[_Unit] = []
    for match in sorted(root.glob(entry.pattern)):
        rel = match.relative_to(root)
        if match.is_symlink() or _is_protected(match):
            continue
        if any(
            fnmatch.fnmatch(part, pat) for part in rel.parts for pat in entry.exclude
        ):
            continue
        if not (match.is_file() or match.is_dir()):
            continue
        if match.is_dir() and (match / ".git").exists():
            continue
        unit = _measure(match)
        if unit.files:
            out.append(unit)
    return out


def _sha256(stream_chunks: Iterator[bytes]) -> str:
    digest = hashlib.sha256()
    for chunk in stream_chunks:
        digest.update(chunk)
    return digest.hexdigest()


def _file_chunks(path: Path) -> Iterator[bytes]:
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK):
            yield chunk


def _handle_chunks(handle: IO[bytes]) -> Iterator[bytes]:
    while chunk := handle.read(_CHUNK):
        yield chunk


def _gz_chunks(path: Path) -> Iterator[bytes]:
    with gzip.open(path, "rb") as handle:
        while chunk := handle.read(_CHUNK):
            yield chunk


def _write_archive(src: Path, dest: Path) -> None:
    """Compress ``src`` (a file or a directory) to ``dest``, gzip or tar.gz."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        with tarfile.open(dest, "w:gz") as tar:
            for file in _walk(src):
                tar.add(
                    file,
                    arcname=str(Path(src.name) / file.relative_to(src)),
                    recursive=False,
                )
    else:
        with src.open("rb") as raw, gzip.open(dest, "wb") as out:
            shutil.copyfileobj(raw, out, _CHUNK)


def _verify_archive(src: Path, dest: Path) -> None:
    """Read the archive back and compare every source file by sha256."""
    try:
        _compare_archive(src, dest)
    except (EOFError, tarfile.TarError, zlib.error) as exc:
        msg = f"archive verify failed: {dest.name} unreadable ({exc})"
        raise OSError(msg) from exc


def _compare_archive(src: Path, dest: Path) -> None:
    if src.is_dir():
        with tarfile.open(dest, "r:gz") as tar:
            members = {m.name: m for m in tar.getmembers() if m.isfile()}
            for file in _walk(src):
                name = str(Path(src.name) / file.relative_to(src))
                member = members.pop(name, None)
                # extractfile streams the member's bytes into memory and writes no
                # path to disk, so the extraction filter (extract/extractall only)
                # has nothing to constrain; the name is looked up, never joined.
                extracted = tar.extractfile(member) if member else None
                if member is None or extracted is None:
                    msg = f"archive verify failed: {name} missing from {dest.name}"
                    raise OSError(msg)
                if _sha256(_handle_chunks(extracted)) != _sha256(_file_chunks(file)):
                    msg = f"archive verify failed: checksum differs for {name}"
                    raise OSError(msg)
            if members:
                msg = f"archive verify failed: {dest.name} holds files the source lacks"
                raise OSError(msg)
        return
    if _sha256(_gz_chunks(dest)) != _sha256(_file_chunks(src)):
        msg = f"archive verify failed: checksum differs for {src.name}"
        raise OSError(msg)


def _archive_dest(
    archive_root: Path, host: str, entry: FileRetentionEntry, rel: Path, is_dir: bool
) -> Path:
    suffix = ".tar.gz" if is_dir else ".gz"
    return archive_root / host / entry.name / rel.parent / (rel.name + suffix)


def _ensure_archive(src: Path, dest: Path) -> int:
    """Leave a verified copy of ``src`` at ``dest``; reuse one that still verifies."""
    if dest.exists():
        try:
            _verify_archive(src, dest)
        except OSError:
            dest.unlink()
        else:
            return dest.stat().st_size
    try:
        _write_archive(src, dest)
        _verify_archive(src, dest)
    except OSError:
        dest.unlink(missing_ok=True)
        raise
    return dest.stat().st_size


def _has_marker(path: Path, marker: str) -> bool:
    """True when the sync client has set ``marker`` (an xattr name) on ``path``."""
    if hasattr(os, "getxattr"):
        try:
            os.getxattr(path, marker)
        except OSError:
            return False
        return True
    done = subprocess.run(
        ["xattr", "-p", marker, str(path)],
        capture_output=True,
        check=False,
    )
    return done.returncode == 0


def _remove(unit: Path) -> None:
    if unit.is_dir():
        shutil.rmtree(unit)
    else:
        unit.unlink()


def _resolve(path: str) -> Path | None:
    expanded = os.path.expandvars(os.path.expanduser(path))
    if "$" in expanded:
        return None
    return Path(expanded)


class NodeFileRetention:
    """Retires aged files per the contract's ``file_retention`` entries."""

    def handle(self, request: FileRetentionRequest) -> FileRetentionResult:
        host = request.host or default_host()
        archive_root = Path(request.archive_root) if request.archive_root else None
        items = [
            self._run_entry(entry, request, host, archive_root)
            for entry in request.entries
            if not request.only or entry.name in request.only
        ]
        return FileRetentionResult(
            host=host,
            dry_run=request.dry_run,
            items=items,
            files_removed=sum(i.files_removed for i in items),
            bytes_removed=sum(i.bytes_removed for i in items),
            files_archived=sum(i.files_archived for i in items),
            bytes_archived=sum(i.bytes_archived for i in items),
        )

    def _run_entry(
        self,
        entry: FileRetentionEntry,
        request: FileRetentionRequest,
        host: str,
        archive_root: Path | None,
    ) -> FileRetentionItemResult:
        if not entry.enabled:
            return FileRetentionItemResult(
                name=entry.name, status="disabled", action=entry.action
            )
        root = _resolve(entry.path)
        if root is None or not root.is_dir() or root.is_symlink():
            return FileRetentionItemResult(
                name=entry.name, status="absent", action=entry.action
            )
        note = ""
        if entry.action == "archive" and (
            archive_root is None or not archive_root.is_dir()
        ):
            if not request.dry_run:
                return FileRetentionItemResult(
                    name=entry.name, status="archive_unavailable", action=entry.action
                )
            note = "archive root unavailable: a live run would remove nothing"

        cutoff = (request.now - dt.timedelta(days=entry.keep_days)).timestamp()
        units = _units(root, entry)
        before = sum(u.bytes for u in units)
        counts = {
            "units": 0,
            "files": 0,
            "bytes": 0,
            "afiles": 0,
            "abytes": 0,
            "pending": 0,
            "pbytes": 0,
        }
        error = ""
        for unit in units:
            if unit.newest >= cutoff:
                continue
            try:
                if entry.action == "archive" and archive_root is not None and not note:
                    dest = _archive_dest(
                        archive_root,
                        host,
                        entry,
                        unit.path.relative_to(root),
                        unit.path.is_dir(),
                    )
                    archived = 0
                    if not request.dry_run:
                        archived = _ensure_archive(unit.path, dest)
                    counts["afiles"] += len(unit.files)
                    counts["abytes"] += archived
                    if (
                        not request.dry_run
                        and request.synced_marker
                        and not _has_marker(dest, request.synced_marker)
                    ):
                        counts["pending"] += 1
                        counts["pbytes"] += unit.bytes
                        continue
                if not request.dry_run:
                    _remove(unit.path)
            except OSError as exc:
                error = f"{unit.path.name}: {exc}"
                break
            counts["units"] += 1
            counts["files"] += len(unit.files)
            counts["bytes"] += unit.bytes
        status: Literal["ok", "dry_run", "error"] = (
            "error" if error else "dry_run" if request.dry_run else "ok"
        )
        return FileRetentionItemResult(
            name=entry.name,
            status=status,
            action=entry.action,
            units_removed=counts["units"],
            files_removed=counts["files"],
            bytes_removed=counts["bytes"],
            files_archived=counts["afiles"],
            bytes_archived=counts["abytes"],
            bytes_before=before,
            bytes_after=before - counts["bytes"],
            units_pending_sync=counts["pending"],
            bytes_pending_sync=counts["pbytes"],
            error=error,
            note=note,
        )
