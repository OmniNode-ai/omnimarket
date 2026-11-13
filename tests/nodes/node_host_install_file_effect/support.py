# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Builders and fakes shared by the host install file effect tests (OMN-23957)."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml

from omnimarket.nodes.node_host_install_file_effect.models import (
    ModelHostInstallFileRequest,
)
from omnimarket.nodes.node_host_install_file_effect.overlay import (
    ModelHostInstallFileOverlay,
)

NODE_DIR = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_host_install_file_effect"
)
RUN_ID = uuid4()
HOST = "host-a"
UNIT = "unit-a"


def overlay() -> ModelHostInstallFileOverlay:
    document = yaml.safe_load((NODE_DIR / "overlay.example.yaml").read_text())
    return ModelHostInstallFileOverlay.model_validate(document)


def request(
    mode: str = "snapshot",
    paths: tuple[str, ...] = ("/tmp/a.service", "/tmp/a.timer"),
    **cells: Any,
) -> ModelHostInstallFileRequest:
    return ModelHostInstallFileRequest(
        correlation_id=cells.pop("correlation_id", RUN_ID),
        host=cells.pop("host", HOST),
        unit=cells.pop("unit", UNIT),
        mode=mode,
        paths=paths,
        **cells,
    )


class FakeHostInstallFiles:
    """In-memory fake of ProtocolHostInstallFiles for the effect tests."""

    def __init__(self) -> None:
        self._files: dict[str, str] = {}
        self._dirs: set[str] = set()

    def exists(self, path: str) -> bool:
        return path in self._files

    def read_text(self, path: str) -> str:
        if path not in self._files:
            raise FileNotFoundError(path)
        return self._files[path]

    def write_text(self, path: str, text: str) -> None:
        self._files[path] = text

    def remove(self, path: str) -> None:
        self._files.pop(path, None)

    def makedirs(self, path: str) -> None:
        self._dirs.add(path)

    def size(self, path: str) -> int:
        if path not in self._files:
            raise FileNotFoundError(path)
        return len(self._files[path].encode("utf-8"))

    def seed(self, path: str, text: str) -> None:
        self._files[path] = text

    def manifest_path(self, backup_dir: str) -> str:
        return f"{backup_dir}/manifest.json"

    def set_manifest(self, backup_dir: str, data: dict[str, Any]) -> None:
        import json

        self._files[f"{backup_dir}/manifest.json"] = json.dumps(data)

    def get_manifest(self, backup_dir: str) -> dict[str, Any] | None:
        import json

        key = f"{backup_dir}/manifest.json"
        if key not in self._files:
            return None
        return json.loads(self._files[key])
