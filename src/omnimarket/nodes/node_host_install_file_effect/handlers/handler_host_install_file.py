# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The handler of node_host_install_file_effect (OMN-23957).

``HandlerHostInstallFile`` runs the three file steps of a host unit install over the installed unit files of one unit,
taking the overlay and the file I/O as constructor arguments. ``LocalHostInstallFiles`` is the real implementation on
``pathlib`` and ``os.replace``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from omnimarket.nodes.node_host_install_file_effect.models.model_host_install_file import (
    ModelHostInstallFileRequest,
    ModelHostInstallFileResult,
)
from omnimarket.nodes.node_host_install_file_effect.overlay import (
    ModelHostInstallFileOverlay,
    load_overlay,
)
from omnimarket.nodes.node_host_install_file_effect.protocols.protocol_host_install_files import (
    ProtocolHostInstallFiles,
)
from omnimarket.nodes.node_host_install_readback_compute.digest import digest_text


class LocalHostInstallFiles:
    """The real file I/O on ``pathlib`` and ``os.replace``."""

    def exists(self, path: str) -> bool:
        return Path(path).is_file()

    def read_text(self, path: str) -> str:
        return Path(path).read_text(encoding="utf-8")

    def write_text(self, path: str, text: str) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        sibling = target.with_name(target.name + ".tmp")
        sibling.write_text(text, encoding="utf-8")
        os.replace(sibling, target)

    def remove(self, path: str) -> None:
        Path(path).unlink(missing_ok=True)

    def makedirs(self, path: str) -> None:
        Path(path).mkdir(parents=True, exist_ok=True)

    def size(self, path: str) -> int:
        return Path(path).stat().st_size


class HandlerHostInstallFile:
    """Runs one file step of a host unit install."""

    def __init__(
        self,
        overlay: ModelHostInstallFileOverlay | None = None,
        files: ProtocolHostInstallFiles | None = None,
    ) -> None:
        self._configured_overlay = overlay
        self._files = files if files is not None else LocalHostInstallFiles()

    @property
    def _overlay(self) -> ModelHostInstallFileOverlay:
        if self._configured_overlay is None:
            self._configured_overlay = load_overlay()
        return self._configured_overlay

    def handle(
        self, request: ModelHostInstallFileRequest
    ) -> ModelHostInstallFileResult:
        backup_dir = f"{self._overlay.backup_root}/{request.correlation_id}"
        if request.mode == "snapshot":
            return self._snapshot(request, backup_dir)
        if request.mode == "read":
            return self._read(request, backup_dir)
        return self._restore(request, backup_dir)

    def _failure(
        self, request: ModelHostInstallFileRequest, detail: str
    ) -> ModelHostInstallFileResult:
        return ModelHostInstallFileResult(
            correlation_id=request.correlation_id,
            host=request.host,
            unit=request.unit,
            mode=request.mode,
            status="failure",
            present=False,
            text="",
            digest="",
            backup_dir=f"{self._overlay.backup_root}/{request.correlation_id}",
            detail=detail,
        )

    def _concatenated(
        self, request: ModelHostInstallFileRequest
    ) -> tuple[str, bool, bool, str]:
        """The existing files' text joined by a newline, whether all exist, whether any exists, and a failure cause."""
        texts: list[str] = []
        present = True
        for path in request.paths:
            if not self._files.exists(path):
                present = False
                continue
            try:
                text = self._files.read_text(path)
            except OSError:
                return "", False, False, f"unreadable file: {path}"
            if len(text.encode("utf-8")) > self._overlay.max_text_bytes:
                return "", False, False, f"text exceeds max_text_bytes: {path}"
            texts.append(text)
        return "\n".join(texts), present, bool(texts), ""

    def _snapshot(
        self, request: ModelHostInstallFileRequest, backup_dir: str
    ) -> ModelHostInstallFileResult:
        text, present, any_present, error = self._concatenated(request)
        if error:
            return self._failure(request, error)
        self._files.makedirs(backup_dir)
        manifest: dict[str, bool] = {}
        for index, path in enumerate(request.paths):
            existed = self._files.exists(path)
            manifest[path] = existed
            if existed:
                self._files.write_text(
                    f"{backup_dir}/{index}{self._overlay.backup_suffix}",
                    self._files.read_text(path),
                )
        self._files.write_text(
            f"{backup_dir}/{self._overlay.manifest_name}",
            json.dumps(manifest, indent=2),
        )
        digest = (
            digest_text(text, self._overlay.digest_algorithm) if any_present else ""
        )
        return ModelHostInstallFileResult(
            correlation_id=request.correlation_id,
            host=request.host,
            unit=request.unit,
            mode="snapshot",
            status="success",
            present=present,
            text=text,
            digest=digest,
            backup_dir=backup_dir,
            detail="",
        )

    def _read(
        self, request: ModelHostInstallFileRequest, backup_dir: str
    ) -> ModelHostInstallFileResult:
        text, present, any_present, error = self._concatenated(request)
        if error:
            return self._failure(request, error)
        digest = (
            digest_text(text, self._overlay.digest_algorithm) if any_present else ""
        )
        return ModelHostInstallFileResult(
            correlation_id=request.correlation_id,
            host=request.host,
            unit=request.unit,
            mode="read",
            status="success",
            present=present,
            text=text,
            digest=digest,
            backup_dir=backup_dir,
            detail="",
        )

    def _restore(
        self, request: ModelHostInstallFileRequest, backup_dir: str
    ) -> ModelHostInstallFileResult:
        manifest_path = f"{backup_dir}/{self._overlay.manifest_name}"
        if not self._files.exists(manifest_path):
            return self._failure(request, "missing manifest")
        manifest = json.loads(self._files.read_text(manifest_path))
        for index, path in enumerate(request.paths):
            backup_file = f"{backup_dir}/{index}{self._overlay.backup_suffix}"
            if manifest.get(path, False):
                self._files.write_text(path, self._files.read_text(backup_file))
            else:
                self._files.remove(path)
        text, present, any_present, error = self._concatenated(request)
        if error:
            return self._failure(request, error)
        digest = (
            digest_text(text, self._overlay.digest_algorithm) if any_present else ""
        )
        return ModelHostInstallFileResult(
            correlation_id=request.correlation_id,
            host=request.host,
            unit=request.unit,
            mode="restore",
            status="success",
            present=present,
            text="",
            digest=digest,
            backup_dir=backup_dir,
            detail="",
        )
