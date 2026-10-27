# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read only immutable objects from a runtime-provided repository mirror."""

from __future__ import annotations

import os
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Protocol

from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env

from omnimarket.nodes.node_context_source_git_effect.models.model_git_source import (
    ModelGitBlob,
    ModelGitSnapshot,
)


class GitSourceReadError(RuntimeError):
    """A missing, unsupported or unreadable pinned source (no partial answer)."""


class ProtocolGitObjectReader(Protocol):
    """The effect's injected object-store boundary."""

    def read(
        self,
        repository_path: Path,
        commit_sha: str,
        paths: tuple[str, ...],
        *,
        timeout_seconds: float,
    ) -> ModelGitSnapshot: ...


class GitObjectReader:
    """Use Git plumbing without reading checkout files, fetching or credentials."""

    def read(
        self,
        repository_path: Path,
        commit_sha: str,
        paths: tuple[str, ...],
        *,
        timeout_seconds: float,
    ) -> ModelGitSnapshot:
        deadline = time.monotonic() + timeout_seconds

        def git(*args: str) -> bytes:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise GitSourceReadError("pinned Git read exceeded its deadline")
            try:
                return subprocess.run(
                    ["git", "--no-replace-objects", "-C", str(repository_path), *args],
                    check=True,
                    capture_output=True,
                    timeout=remaining,
                    env=scrub_git_location_env(os.environ),
                ).stdout
            except (OSError, subprocess.SubprocessError) as exc:
                # Git stderr can contain deployment paths. Keep it out of results.
                raise GitSourceReadError("pinned Git object read failed") from exc

        if git("cat-file", "-t", commit_sha).strip() != b"commit":
            raise GitSourceReadError("pin does not name a commit")
        committed_at = datetime.fromisoformat(
            git("show", "-s", "--format=%cI", commit_sha).decode().strip()
        )
        tree: dict[str, tuple[str, str, str]] = {}
        for record in git("ls-tree", "-r", "-z", "--full-tree", commit_sha).split(
            b"\x00"
        ):
            if not record:
                continue
            metadata, path_bytes = record.split(b"\t", 1)
            mode, kind, sha = metadata.decode("ascii").split()
            tree[path_bytes.decode("utf-8")] = (mode, kind, sha)
        blobs: list[ModelGitBlob] = []
        for path in paths:
            item = tree.get(path)
            if item is None or item[0] not in ("100644", "100755") or item[1] != "blob":
                raise GitSourceReadError(
                    "requested regular file is absent from pinned tree"
                )
            blobs.append(
                ModelGitBlob(
                    path=path,
                    blob_sha=item[2],
                    raw_bytes=git("cat-file", "blob", item[2]),
                )
            )
        return ModelGitSnapshot(
            commit_sha=commit_sha,
            committed_at=committed_at,
            tree_paths=tuple(tree),
            blobs=tuple(blobs),
        )
