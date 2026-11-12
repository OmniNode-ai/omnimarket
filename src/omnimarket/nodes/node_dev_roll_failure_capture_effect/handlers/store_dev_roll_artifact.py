# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Artifact stores of node_dev_roll_failure_capture_effect (OMN-24013).

Two stores behind one protocol: an in-memory dict for tests and a file store
that writes atomically (temp file in the target directory, then os.replace).
``store_from_policy`` builds the right one from the overlay's capture policy.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_dev_roll_failure_capture_effect.models import (
    ModelDevRollCapturePolicy,
)
from omnimarket.nodes.node_dev_roll_failure_capture_effect.protocols import (
    ProtocolDevRollArtifactStore,
)


def _validate_key(key: str) -> None:
    """Refuse a key that is empty, absolute or has a ".." or empty part."""
    if not key:
        raise ValueError("artifact key is empty")
    if key.startswith("/"):
        raise ValueError(f"artifact key {key!r} is absolute")
    parts = key.split("/")
    for part in parts:
        if part == "":
            raise ValueError(f"artifact key {key!r} has an empty path part")
        if part == "..":
            raise ValueError(f"artifact key {key!r} has a '..' path part")


class InMemoryDevRollArtifactStore:
    """Dict-backed store; the honest one for tests."""

    def __init__(self) -> None:
        self._data: dict[str, bytes] = {}

    def write(self, key: str, content: bytes) -> str:
        _validate_key(key)
        self._data[key] = content
        return hashlib.sha256(content).hexdigest()

    def read(self, key: str) -> bytes | None:
        _validate_key(key)
        return self._data.get(key)


class FileDevRollArtifactStore:
    """File store: atomic write (temp file in the target directory, os.replace)."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def _path(self, key: str) -> Path:
        _validate_key(key)
        return self._root / key

    def write(self, key: str, content: bytes) -> str:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(content).hexdigest()
        fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(content)
            os.replace(tmp_name, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)
            raise
        return digest

    def read(self, key: str) -> bytes | None:
        path = self._path(key)
        if not path.is_file():
            return None
        return path.read_bytes()


def store_from_policy(
    policy: ModelDevRollCapturePolicy, environ: Mapping[str, str]
) -> ProtocolDevRollArtifactStore:
    """Build the store the overlay's capture policy names."""
    store = policy.store
    if store.backend == "in_memory":
        return InMemoryDevRollArtifactStore()
    try:
        root_env = environ[store.home_env]
    except KeyError as exc:
        raise NodeOverlayError(
            f"environment variable {store.home_env!r} is not set"
        ) from exc
    root = Path(root_env) / store.relpath
    return FileDevRollArtifactStore(root)
