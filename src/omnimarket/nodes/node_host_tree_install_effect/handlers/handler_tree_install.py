# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Stage and compare the entire declared source tree before replacing a host tree.

There is no file or extension selection. Symlinks are copied as links, never
followed; modes and empty directories participate in the equality check. A failed
copy leaves the installed tree alone. Replacement keeps the previous tree beside
the install for recovery and restores it if the final rename or readback fails.
This effect does not install units, restart services or change acting receipts.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import stat
import tempfile
from pathlib import Path
from typing import TypedDict

import yaml

from omnimarket.handlers.node_overlay_reader import resolve_overlay_file
from omnimarket.nodes.node_host_tree_install_effect.models.model_tree_install import (
    ModelTreeInstallOverlay,
    ModelTreeInstallRequest,
    ModelTreeInstallResult,
)

NODE_DIR = Path(__file__).resolve().parents[1]


class _TreeReceipt(TypedDict):
    source: str
    destination: str
    tree_sha256: str
    file_count: int


def tree_snapshot(root: Path) -> dict[str, tuple[str, int, str]]:
    """Record every entry without following symlinks or opening special files."""
    result: dict[str, tuple[str, int, str]] = {}

    def visit(path: Path) -> None:
        info = path.lstat()
        relative = path.relative_to(root).as_posix()
        mode = stat.S_IMODE(info.st_mode)
        if stat.S_ISLNK(info.st_mode):
            result[relative] = ("link", mode, os.readlink(path))
        elif stat.S_ISDIR(info.st_mode):
            result[relative] = ("directory", mode, "")
            for child in sorted(path.iterdir()):
                visit(child)
        elif stat.S_ISREG(info.st_mode):
            with path.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            result[relative] = ("file", mode, digest)
        else:
            raise ValueError("source tree contains a special file")

    visit(root)
    return result


def destination_only(
    source: dict[str, tuple[str, int, str]],
    installed: dict[str, tuple[str, int, str]],
) -> dict[str, tuple[str, int, str]]:
    """Keep generated install entries unless the source replaces their ancestor."""
    return {
        relative: entry
        for relative, entry in installed.items()
        if relative not in source
        and all(
            source.get(parent.as_posix(), ("directory", 0, ""))[0] == "directory"
            for parent in Path(relative).parents
        )
    }


def preserve_entries(
    installed: Path,
    staged: Path,
    entries: dict[str, tuple[str, int, str]],
) -> None:
    """Copy destination-only entries without traversing a copied symlink."""
    directories: list[tuple[Path, int]] = []
    for relative, (kind, mode, target) in entries.items():
        source, destination = installed / relative, staged / relative
        if kind == "directory":
            destination.mkdir()
            directories.append((destination, mode))
        elif kind == "link":
            destination.symlink_to(target)
        else:
            shutil.copy2(source, destination, follow_symlinks=False)
    for directory, mode in reversed(directories):
        directory.chmod(mode)


def declared_tree(root: Path, relative: str) -> Path:
    """Resolve a layout entry without permitting traversal or symlinked parents."""
    path = Path(relative)
    if (
        not relative
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in relative.split("/"))
        or "\\" in relative
    ):
        raise ValueError("tree layout must name a relative directory without traversal")
    current = root
    for part in path.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("tree layout must not traverse symlinks")
    if current.exists() and not current.is_dir():
        raise ValueError("tree layout names an existing non-directory")
    return current


class HandlerTreeInstall:
    """``handle(request) -> result`` for the declared install command."""

    def handle(self, request: ModelTreeInstallRequest) -> ModelTreeInstallResult:
        try:
            overlay = request.overlay
            if overlay is None:
                path = resolve_overlay_file(
                    NODE_DIR.name,
                    NODE_DIR / "overlay.yaml",
                    env_var="ONEX_HOST_TREE_INSTALL_OVERLAY",
                )
                overlay = ModelTreeInstallOverlay.model_validate(
                    yaml.safe_load(path.read_text(encoding="utf-8"))
                )
            source = declared_tree(
                request.source_root.resolve(strict=True), overlay.source_relative
            )
            destination = declared_tree(
                request.install_root.resolve(), overlay.destination_relative
            )
            if (
                source == destination
                or source.is_relative_to(destination)
                or destination.is_relative_to(source)
            ):
                raise ValueError("source and destination trees must not overlap")
            source_entries = tree_snapshot(source)
            if not any(entry[0] == "file" for entry in source_entries.values()):
                raise ValueError("source tree contains no regular files")

            def plan() -> tuple[
                dict[str, tuple[str, int, str]],
                dict[str, tuple[str, int, str]],
                _TreeReceipt,
            ]:
                prior = tree_snapshot(destination) if destination.is_dir() else {}
                expected = destination_only(source_entries, prior) | source_entries
                receipt: _TreeReceipt = {
                    "source": str(source),
                    "destination": str(destination),
                    "tree_sha256": hashlib.sha256(
                        json.dumps(expected, sort_keys=True).encode()
                    ).hexdigest(),
                    "file_count": sum(
                        entry[0] == "file" for entry in expected.values()
                    ),
                }
                return prior, expected, receipt

            if request.mode in {"dry-run", "verify"}:
                prior, expected, receipt = plan()
                if request.mode == "dry-run":
                    return ModelTreeInstallResult(status="dry-run", **receipt)
                equal = prior == expected
                return ModelTreeInstallResult(
                    status="verified" if equal else "refused",
                    detail="" if equal else "installed tree differs from source",
                    **receipt,
                )
            destination.parent.mkdir(parents=True, exist_ok=True)
            lock_path = destination.parent / f".{destination.name}.install.lock"
            with lock_path.open("a") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return ModelTreeInstallResult(
                        status="locked", destination=str(destination)
                    )
                prior, expected, receipt = plan()
                if prior == expected:
                    return ModelTreeInstallResult(status="unchanged", **receipt)
                with tempfile.TemporaryDirectory(
                    prefix=f".{destination.name}.stage-", dir=destination.parent
                ) as scratch:
                    staged = Path(scratch) / "tree"
                    shutil.copytree(source, staged, symlinks=True)
                    preserve_entries(
                        destination, staged, destination_only(source_entries, prior)
                    )
                    if (
                        tree_snapshot(staged) != expected
                        or tree_snapshot(source) != source_entries
                    ):
                        raise ValueError(
                            "source changed or staged tree differs from source"
                        )
                    if prior and tree_snapshot(destination) != prior:
                        raise ValueError("installed tree changed while staging")
                    previous = destination.parent / (Path(scratch).name + ".previous")
                    had_previous = destination.exists()
                    if had_previous:
                        destination.rename(previous)
                    try:
                        staged.rename(destination)
                        if tree_snapshot(destination) != expected:
                            raise ValueError(
                                "installed tree differs from staged source"
                            )
                    except (OSError, ValueError) as install_error:
                        try:
                            if destination.exists():
                                shutil.rmtree(destination)
                            if had_previous:
                                previous.rename(destination)
                        except OSError as restore_error:
                            return ModelTreeInstallResult(
                                status="failed",
                                previous_tree=str(previous) if had_previous else "",
                                detail=f"install failed: {install_error}; restore failed: {restore_error}",
                                **receipt,
                            )
                        raise
                    return ModelTreeInstallResult(
                        status="installed",
                        previous_tree=str(previous) if had_previous else "",
                        **receipt,
                    )
        except (OSError, ValueError, yaml.YAMLError) as exc:
            return ModelTreeInstallResult(status="refused", detail=str(exc))
