# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Handler for KB Repowise index effect — EFFECT node.

Clones/pulls OmniNode-ai/knowledge-base into a temp directory, invokes the
Repowise indexer CLI to update the index, and publishes a completion event
with the HEAD commit SHA and entry count.

[OMN-11914]
"""

from __future__ import annotations

import logging
import subprocess
import tempfile
from pathlib import Path

from omnimarket.nodes.node_kb_repowise_index_effect.models.model_index_request import (
    ModelKBRepoIndexRequest,
)
from omnimarket.nodes.node_kb_repowise_index_effect.models.model_index_result import (
    ModelKBRepoIndexResult,
)

logger = logging.getLogger(__name__)

_REPOWISE_CLI = "repowise"


def _get_commit_sha(repo_dir: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_dir), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip() or None
    except subprocess.CalledProcessError:
        return None


def _parse_entry_count(output: str) -> int:
    """Extract integer entry count from repowise CLI stdout."""
    for line in reversed(output.splitlines()):
        line = line.strip()
        for token in line.split():
            try:
                return int(token)
            except ValueError:
                continue
    return 0


class HandlerKBRepoWiseIndex:
    """EFFECT handler — clones KB repo and triggers Repowise reindexing."""

    async def handle(
        self, *, request: ModelKBRepoIndexRequest
    ) -> ModelKBRepoIndexResult:
        if request.dry_run:
            logger.info(
                "Dry-run: would clone %s and invoke repowise index", request.kb_repo
            )
            return ModelKBRepoIndexResult(success=True)

        with tempfile.TemporaryDirectory() as tmpdir:
            kb_dir = Path(tmpdir) / "knowledge-base"

            logger.info("Cloning %s ...", request.kb_repo)
            try:
                subprocess.run(
                    ["gh", "repo", "clone", request.kb_repo, str(kb_dir)],
                    check=True,
                    capture_output=True,
                    text=True,
                )
            except subprocess.CalledProcessError as exc:
                logger.error("Failed to clone %s: %s", request.kb_repo, exc.stderr)
                return ModelKBRepoIndexResult(
                    success=False,
                    error=f"Clone failed: {exc.stderr}",
                )

            commit_sha = _get_commit_sha(kb_dir)
            logger.info("Cloned at commit %s", commit_sha)

            logger.info("Invoking repowise index on %s ...", kb_dir)
            try:
                index_result = subprocess.run(
                    [_REPOWISE_CLI, "index", str(kb_dir)],
                    capture_output=True,
                    text=True,
                    check=True,
                )
            except subprocess.CalledProcessError as exc:
                logger.error("Repowise index failed: %s", exc.stderr)
                return ModelKBRepoIndexResult(
                    success=False,
                    commit_sha=commit_sha,
                    error=f"Repowise index failed: {exc.stderr}",
                )

            entry_count = _parse_entry_count(index_result.stdout)
            logger.info(
                "Repowise index complete — %d entries, commit %s",
                entry_count,
                commit_sha,
            )

        return ModelKBRepoIndexResult(
            success=True,
            commit_sha=commit_sha,
            entry_count=entry_count,
        )
