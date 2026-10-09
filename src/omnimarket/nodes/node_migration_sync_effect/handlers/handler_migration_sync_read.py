# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read the migration trees the plan node decides from (OMN-20687).

Reads, in order: the omnimarket source tree (resolved as the old script resolved
it), every ``*/migrations/*.sql`` file under it, the vendored ``*.sql`` files, and
the application-migrations manifest. A read that fails is named in the inventory's
``failure``; it is never read as an empty tree.
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
import re
from pathlib import Path

from omnimarket.models.migration_sync import (
    ModelMigrationSyncFile,
    ModelMigrationSyncInventory,
    ModelMigrationSyncManifestRow,
)

from ..models import ModelMigrationSyncReadRequest

_SOURCE_PATH = re.compile(r".*/migrations/.*\.sql", re.DOTALL)
_MANIFEST_SHA_COLUMN = 5


def read_file_bytes(path: Path) -> bytes:
    """The one place a migration file's bytes are read."""
    return path.read_bytes()


def _nodes_dir(request: ModelMigrationSyncReadRequest) -> Path | None:
    """The omnimarket nodes directory: explicit source, then registry_root, then the package."""
    if request.omnimarket_src:
        candidate = Path(request.omnimarket_src) / "src" / "omnimarket" / "nodes"
        if candidate.is_dir():
            return candidate
    if request.registry_root:
        candidate = (
            Path(request.registry_root) / "omnimarket" / "src" / "omnimarket" / "nodes"
        )
        if candidate.is_dir():
            return candidate
    if request.allow_installed_package:
        spec = importlib.util.find_spec("omnimarket")
        if spec and spec.submodule_search_locations:
            candidate = Path(next(iter(spec.submodule_search_locations))) / "nodes"
            if candidate.is_dir():
                return candidate
    return None


def _regular_files(root: Path) -> list[Path]:
    """Regular files below ``root`` without following symlinks, like ``find -type f``."""
    found: list[Path] = []
    for directory, _, names in os.walk(root, followlinks=False):
        for name in names:
            path = Path(directory) / name
            if path.is_file() and not path.is_symlink():
                found.append(path)
    return found


def _sha256(path: Path) -> str:
    return hashlib.sha256(read_file_bytes(path)).hexdigest()


def _manifest_rows(path: Path) -> tuple[ModelMigrationSyncManifestRow, ...]:
    text = read_file_bytes(path).decode("utf-8", errors="replace")
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    rows = []
    for line in lines:
        columns = line.split("\t")
        rows.append(
            ModelMigrationSyncManifestRow(
                artifact_path=columns[0],
                declared_sha256=(
                    columns[_MANIFEST_SHA_COLUMN]
                    if len(columns) > _MANIFEST_SHA_COLUMN
                    else ""
                ),
            )
        )
    return tuple(rows)


class HandlerMigrationSyncRead:
    """Read the source migrations, the vendored copies and the manifest."""

    def handle(
        self, request: ModelMigrationSyncReadRequest
    ) -> ModelMigrationSyncInventory:
        dest_root = Path(request.dest_root)
        nodes_dir = _nodes_dir(request)
        if nodes_dir is None:
            return ModelMigrationSyncInventory(
                resolved=False, dest_root=request.dest_root
            )

        def failed(what: str, path: Path, exc: OSError) -> ModelMigrationSyncInventory:
            return ModelMigrationSyncInventory(
                resolved=True,
                nodes_dir=str(nodes_dir),
                dest_root=request.dest_root,
                failure=f"cannot read {what} {path}: {exc}",
            )

        source_files = []
        try:
            sources = sorted(
                (
                    p
                    for p in _regular_files(nodes_dir)
                    if _SOURCE_PATH.fullmatch(str(p))
                ),
                key=str,
            )
        except OSError as exc:
            return failed("source tree", nodes_dir, exc)
        for path in sources:
            try:
                digest = _sha256(path)
            except OSError as exc:
                return failed("source file", path, exc)
            source_files.append(
                ModelMigrationSyncFile(
                    relative_path=f"{path.parent.parent.name}/{path.name}",
                    sha256=digest,
                    source_path=str(path),
                )
            )

        vendored_files = []
        if dest_root.is_dir():
            try:
                vendored = sorted(
                    (p for p in _regular_files(dest_root) if p.name.endswith(".sql")),
                    key=lambda p: p.relative_to(dest_root).as_posix(),
                )
            except OSError as exc:
                return failed("vendored tree", dest_root, exc)
            for path in vendored:
                try:
                    digest = _sha256(path)
                except OSError as exc:
                    return failed("vendored file", path, exc)
                vendored_files.append(
                    ModelMigrationSyncFile(
                        relative_path=path.relative_to(dest_root).as_posix(),
                        sha256=digest,
                    )
                )

        manifest = Path(request.manifest_path)
        manifest_present = manifest.is_file()
        try:
            rows = _manifest_rows(manifest) if manifest_present else ()
        except OSError as exc:
            return failed("manifest", manifest, exc)

        return ModelMigrationSyncInventory(
            resolved=True,
            nodes_dir=str(nodes_dir),
            dest_root=request.dest_root,
            source_files=tuple(source_files),
            vendored_files=tuple(vendored_files),
            manifest_present=manifest_present,
            manifest_rows=rows,
        )
