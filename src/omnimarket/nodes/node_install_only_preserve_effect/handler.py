# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Copy only missing declared node directories into a disposable install stage."""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path

from omnimarket.handlers.node_overlay_reader import load_structured_overlay

from .models import (
    ModelInstallOnlyOverlay,
    ModelInstallOnlyRequest,
    ModelInstallOnlyResult,
)

OVERLAY_ID = "node_install_only_preserve_effect"


def copy_node(source: Path, destination: Path) -> None:
    """Copy to a sibling first; failed copies never leave a partial staged node."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".install-only-", dir=destination.parent
    ) as scratch:
        pending = Path(scratch) / "node"
        shutil.copytree(source, pending)
        pending.rename(destination)


def node_path(root: Path, relative: str) -> Path:
    path = root / relative
    if not path.resolve().is_relative_to(root):
        raise ValueError("declared node path escapes its install root")
    for part in (path, *path.parents):
        if part == root:
            break
        if part.is_symlink():
            raise ValueError("declared node paths must not traverse symlinks")
    if path.exists() and not path.is_dir():
        raise ValueError("declared node path exists but is not a directory")
    return path


class HandlerInstallOnlyPreserve:
    """The caller can inject declarations and the filesystem copy operation."""

    def __init__(
        self,
        *,
        overlay: ModelInstallOnlyOverlay | None = None,
        copy_tree: Callable[[Path, Path], None] = copy_node,
    ) -> None:
        self.overlay = overlay
        self.copy_tree = copy_tree

    def handle(self, request: ModelInstallOnlyRequest) -> ModelInstallOnlyResult:
        overlay = load_structured_overlay(
            OVERLAY_ID,
            ModelInstallOnlyOverlay,
            explicit=request.overlay if request.overlay is not None else self.overlay,
            file_env="INSTALL_ONLY_NODES_OVERLAY",
        )
        live, stage = (
            request.live_root.resolve(strict=True),
            request.stage_root.resolve(strict=True),
        )
        if not live.is_dir() or not stage.is_dir():
            raise ValueError("live and stage roots must be directories")
        if live == stage or live.is_relative_to(stage):
            raise ValueError("stage must not equal or contain the live install root")
        preserved: list[str] = []
        present: list[str] = []
        unavailable: list[str] = []
        pending: list[tuple[str, Path, Path]] = []
        # Validate the whole declaration before writing any staged node.
        for relative in overlay.nodes:
            destination = node_path(stage, relative)
            if destination.exists():
                present.append(relative)
                continue
            source = node_path(live, relative)
            if not source.exists():
                unavailable.append(relative)
                continue
            if any(path.is_symlink() for path in source.rglob("*")):
                raise ValueError("install-only node contents must not contain symlinks")
            pending.append((relative, source, destination))
        for relative, source, destination in pending:
            self.copy_tree(source, destination)
            preserved.append(relative)
        return ModelInstallOnlyResult(
            preserved=tuple(preserved),
            stage_present=tuple(present),
            unavailable=tuple(unavailable),
        )
