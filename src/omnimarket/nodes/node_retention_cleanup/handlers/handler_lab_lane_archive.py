# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Archive complete context outputs and read them back before source removal.

Clean lane code is recoverable from the checked remote refs; its revision and
object-store location are recorded. Contexts include static ledger snapshots,
receipts and reports, all archived here, including names generic history rules
protect. No symlink is followed. The live context is protected by process checks
in the calling retention handler.
"""

from __future__ import annotations

import hashlib
import json
import os
import tarfile
import tempfile
from pathlib import Path
from typing import IO

from omnimarket.handlers.lab_retention_tree_safety import _inside

from ..models.model_lab_lane_archive import (
    ModelLabLaneArchiveRequest,
    ModelLabLaneArchiveResult,
)


def _context_entries(context: Path) -> dict[str, Path]:
    entries = {"context": context}

    def fail(exc: OSError) -> None:
        raise exc

    for root, dirs, files in os.walk(context, followlinks=False, onerror=fail):
        for name in (*dirs, *files):
            path = Path(root) / name
            entries[str(Path("context") / path.relative_to(context))] = path
    return entries


def _hash_handle(handle: IO[bytes]) -> str:
    digest = hashlib.sha256()
    while chunk := handle.read(1 << 20):
        digest.update(chunk)
    return digest.hexdigest()


def _hash_file(path: Path) -> str:
    with path.open("rb") as handle:
        return _hash_handle(handle)


def _verify_context(context: Path, archive: Path) -> None:
    """Compare the complete member set and every file or link to the source."""
    with tarfile.open(archive, "r:gz") as saved:
        members = {member.name: member for member in saved.getmembers()}
        entries = _context_entries(context)
        if members.keys() != entries.keys():
            raise OSError("context archive member set differs")
        for name, path in entries.items():
            member = members[name]
            if path.is_symlink():
                if not member.issym() or member.linkname != os.readlink(path):
                    raise OSError("context archive symlink differs")
            elif path.is_dir():
                if not member.isdir():
                    raise OSError("context archive directory differs")
            elif path.is_file():
                handle = saved.extractfile(member)
                if handle is None:
                    raise OSError("context archive file missing")
                with handle:
                    digest = _hash_handle(handle)
                if digest != _hash_file(path):
                    raise OSError("context archive checksum differs")
            else:
                raise OSError("context contains a special file")


class HandlerLabLaneArchive:
    """Preserve context history and recovery metadata in the declared local report tree."""

    def handle(self, request: ModelLabLaneArchiveRequest) -> ModelLabLaneArchiveResult:
        root = request.root
        archive = root / request.archive_subdir / request.run.name
        _inside(root, archive)
        if (
            not request.archive_subdir
            or archive.is_relative_to(request.run)
            or archive.is_relative_to(request.context)
        ):
            raise ValueError("archive must be outside source trees")
        archive.mkdir(parents=True, exist_ok=True)
        manifest = archive / "manifest.json"
        context_archive = archive / "context.tar.gz"
        _inside(root, manifest)
        _inside(root, context_archive)
        count = size = 0
        if request.context.exists():
            if context_archive.exists():
                _verify_context(request.context, context_archive)
            else:
                with tempfile.NamedTemporaryFile(
                    dir=archive, prefix=".context-", delete=False
                ) as temporary:
                    staging = Path(temporary.name)
                try:
                    # Store symlinks as links; no source link is traversed.
                    with tarfile.open(staging, "w:gz", dereference=False) as saved:
                        for name, path in _context_entries(request.context).items():
                            saved.add(path, arcname=name, recursive=False)
                    _verify_context(request.context, staging)
                    staging.replace(context_archive)
                    count += sum(
                        path.is_file() and not path.is_symlink()
                        for path in _context_entries(request.context).values()
                    )
                    size += context_archive.stat().st_size
                finally:
                    staging.unlink(missing_ok=True)
        recovery = {
            "schema": "lab_lane_retention.v1",
            "run": request.run.name,
            "head": request.head,
            "git_common_dir": request.git_common_dir,
        }
        if manifest.exists():
            prior = json.loads(manifest.read_text())
            if prior.get("run") != request.run.name or (
                request.head and prior.get("head") != request.head
            ):
                raise OSError("recovery manifest differs")
        else:
            payload = json.dumps(recovery, sort_keys=True) + "\n"
            with manifest.open("x", encoding="utf-8") as handle:
                handle.write(payload)
            if json.loads(manifest.read_text()) != recovery:
                raise OSError("recovery manifest readback differs")
            count += 1
            size += manifest.stat().st_size
        return ModelLabLaneArchiveResult(files_archived=count, bytes_archived=size)
