# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read and write a compaction replay bundle on disk under an explicit root.

The compute node node_context_compaction_manifest_compute is pure; these helpers are the only
file I/O around it and are kept outside the node so its handler stays free of I/O.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path

from omnimarket.nodes.node_context_compaction_manifest_compute.handlers.handler_compaction_manifest import (
    REPLAY_ROOT_ENV,
    ManifestRefusalError,
)
from omnimarket.nodes.node_context_compaction_manifest_compute.models import (
    ModelBundleFile,
)

__all__ = ["read_bundle_files", "replay_root_from_env", "write_bundle_files"]


def replay_root_from_env(env: Mapping[str, str]) -> Path:
    """The bundle root: required, no default."""
    value = env.get(REPLAY_ROOT_ENV, "")
    if not value:
        raise ManifestRefusalError(f"{REPLAY_ROOT_ENV} is required and has no default")
    return Path(value)


def write_bundle_files(root: Path, files: Iterable[ModelBundleFile]) -> None:
    resolved = root.resolve()
    for file in files:
        target = (resolved / file.path).resolve()
        if resolved not in target.parents:
            raise ManifestRefusalError(
                f"bundle path {file.path} resolves outside {REPLAY_ROOT_ENV}"
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(file.content)


def read_bundle_files(root: Path) -> tuple[ModelBundleFile, ...]:
    if not root.is_dir():
        raise ManifestRefusalError(f"{REPLAY_ROOT_ENV} {root} is not a directory")
    return tuple(
        ModelBundleFile(
            path=path.relative_to(root).as_posix(), content=path.read_bytes()
        )
        for path in sorted(root.rglob("*"))
        if path.is_file()
    )
