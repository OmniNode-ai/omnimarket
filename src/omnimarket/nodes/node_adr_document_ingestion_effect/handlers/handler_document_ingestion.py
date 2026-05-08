# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Handler for ADR document ingestion — read-only filesystem effect.

Walks root_paths recursively, finds .md files, computes content hashes,
and extracts git metadata via subprocess. No writes, no LLM, no network.

[OMN-10693]
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import datetime
from pathlib import Path

from omnimarket.nodes.node_adr_document_ingestion_effect.models.model_ingestion_request import (
    ModelIngestionRequest,
)
from omnimarket.nodes.node_adr_document_ingestion_effect.models.model_ingestion_result import (
    ModelDocumentEntry,
    ModelIngestionResult,
)

logger = logging.getLogger(__name__)

_DEFAULT_EXCLUDE_PARTS: frozenset[str] = frozenset(
    ["node_modules", ".git", "omni_worktrees", "__pycache__"]
)


def _compute_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _matches_exclude(path: Path, extra_patterns: list[str] | None) -> bool:
    parts = set(path.parts)
    if parts & _DEFAULT_EXCLUDE_PARTS:
        return True
    path_str = str(path)
    if extra_patterns:
        for pattern in extra_patterns:
            if pattern in path_str:
                return True
    return False


class HandlerDocumentIngestion:
    """EFFECT handler — crawls markdown files and extracts metadata. Read-only."""

    async def handle(self, *, request: ModelIngestionRequest) -> ModelIngestionResult:
        documents: list[ModelDocumentEntry] = []

        for root_str in request.root_paths:
            root = Path(root_str)
            if not root.exists():
                logger.warning("Root path does not exist, skipping: %s", root_str)
                continue

            repo_name = root.name

            for md_file in root.rglob("*.md"):
                rel = md_file.relative_to(root)
                if _matches_exclude(rel, request.exclude_patterns):
                    continue

                try:
                    sha256 = _compute_sha256(md_file)
                    size = md_file.stat().st_size
                    (
                        git_sha,
                        author,
                        created_at_raw,
                        updated_at_raw,
                    ) = await self._git_metadata(md_file, root)

                    created_at = _parse_iso(created_at_raw)
                    updated_at = _parse_iso(updated_at_raw)

                    documents.append(
                        ModelDocumentEntry(
                            source_path=str(rel),
                            repo_name=repo_name,
                            git_sha=git_sha,
                            author=author,
                            created_at=created_at,
                            updated_at=updated_at,
                            file_size_bytes=size,
                            source_content_sha256=sha256,
                        )
                    )
                except Exception:
                    logger.exception("Failed to process file: %s", md_file)

        return ModelIngestionResult(documents=documents)

    async def _git_metadata(
        self, path: Path, cwd: Path
    ) -> tuple[str | None, str | None, str | None, str | None]:
        """Run `git log -1` for the file and return (sha, author, created_at, updated_at).

        updated_at comes from the most recent commit; created_at from the first commit.
        Returns (None, None, None, None) on any failure.
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                "git",
                "log",
                "-1",
                "--format=%H|%an|%aI",
                "--",
                str(path),
                cwd=str(cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()

            if proc.returncode != 0 or not stdout.strip():
                return None, None, None, None

            line = stdout.decode(errors="replace").strip()
            parts = line.split("|", 2)
            if len(parts) != 3:
                return None, None, None, None

            git_sha, author, updated_at = parts

            # First commit date for created_at
            created_at = await self._git_first_commit_date(path, cwd)

            return git_sha or None, author or None, created_at, updated_at or None

        except Exception:
            logger.debug("git metadata unavailable for %s", path)
            return None, None, None, None

    async def _git_first_commit_date(self, path: Path, cwd: Path) -> str | None:
        try:
            proc = await asyncio.create_subprocess_exec(
                "git",
                "log",
                "--diff-filter=A",
                "--follow",
                "--format=%aI",
                "--",
                str(path),
                cwd=str(cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            if proc.returncode != 0 or not stdout.strip():
                return None
            lines = stdout.decode(errors="replace").strip().splitlines()
            return lines[-1].strip() if lines else None
        except Exception:
            return None


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


__all__ = [
    "HandlerDocumentIngestion",
    "_compute_sha256",
    "_matches_exclude",
]
