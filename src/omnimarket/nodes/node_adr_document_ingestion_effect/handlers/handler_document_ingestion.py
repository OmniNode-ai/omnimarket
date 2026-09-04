# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Handler for ADR document ingestion — read-only filesystem effect.

Walks root_paths recursively, finds .md files, computes content hashes,
and extracts git metadata via subprocess. No writes, no LLM, no network.

[OMN-10693]
"""

from __future__ import annotations

import asyncio
import glob
import hashlib
import logging
import os
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


def _workspace_root_for_request(request: ModelIngestionRequest) -> Path | None:
    """Resolve the explicit workspace root needed by workspace-relative globs.

    Absolute roots remain useful for isolated callers and tests. Relative roots
    are never interpreted against the process working directory: they require a
    request-level workspace root or the canonical ``OMNI_HOME`` environment.
    """
    has_relative_root = any(
        not Path(root).expanduser().is_absolute() for root in request.root_paths
    )
    raw_workspace_root = request.workspace_root or (
        os.environ.get("OMNI_HOME") if has_relative_root else None
    )
    if raw_workspace_root is None:
        return None
    workspace_root = Path(raw_workspace_root).expanduser().resolve()
    if not workspace_root.is_dir():
        raise ValueError(f"workspace_root is not a directory: {workspace_root}")
    return workspace_root


def _expand_root_paths(
    root_paths: list[str], workspace_root: Path | None
) -> list[tuple[Path, Path]]:
    """Expand path, directory, and glob roots without consulting ``cwd``.

    A discovery manifest is workspace-scoped. For every match under its
    canonical workspace root, emit source paths relative to that same root so
    downstream segmentation can reopen the exact file through one stable base.
    """
    expanded: list[tuple[Path, Path]] = []
    for raw_root in root_paths:
        raw_path = Path(raw_root).expanduser()
        if raw_path.is_absolute():
            pattern = raw_path
        elif workspace_root is not None:
            pattern = workspace_root / raw_path
        else:
            logger.warning(
                "Relative root path requires workspace_root or OMNI_HOME, skipping: %s",
                raw_root,
            )
            continue

        matches = sorted(Path(match).resolve() for match in glob.glob(str(pattern)))
        if not matches:
            logger.warning("Root path or glob matched nothing, skipping: %s", raw_root)
            continue

        for match in matches:
            if not (match.is_file() or match.is_dir()):
                logger.warning(
                    "Root path is not a file or directory, skipping: %s", match
                )
                continue
            if workspace_root is not None:
                try:
                    match.relative_to(workspace_root)
                except ValueError as exc:
                    raise ValueError(
                        "Ingestion root resolves outside workspace_root: "
                        f"{match} not under {workspace_root}"
                    ) from exc
            source_root = _source_root_for_match(match, workspace_root)
            expanded.append((match, source_root))
    return expanded


def _source_root_for_match(match: Path, workspace_root: Path | None) -> Path:
    if workspace_root is not None:
        try:
            match.relative_to(workspace_root)
        except ValueError:
            pass
        else:
            return workspace_root
    return match if match.is_dir() else match.parent


class HandlerDocumentIngestion:
    """EFFECT handler — crawls markdown files and extracts metadata. Read-only."""

    async def handle(self, payload: ModelIngestionRequest) -> ModelIngestionResult:
        """Crawl root_paths for markdown documents and extract metadata.

        Args:
            payload: Contains root_paths and optional exclude_patterns.
                Named ``payload`` (canonical thin-handler shape) so the
                runtime's single-parameter dispatch passes the validated
                request positionally instead of keyword-fanning the model
                fields.

        Returns:
            ModelIngestionResult with the discovered document entries.
        """
        request = payload
        documents: list[ModelDocumentEntry] = []
        seen_files: set[Path] = set()
        workspace_root = _workspace_root_for_request(request)

        for scan_path, source_root in _expand_root_paths(
            request.root_paths, workspace_root
        ):
            if scan_path.is_file():
                await self._append_document(
                    documents,
                    seen_files,
                    md_file=scan_path,
                    scan_root=scan_path.parent,
                    source_root=source_root,
                    exclude_patterns=request.exclude_patterns,
                )
                continue

            for dirpath, dirnames, filenames in os.walk(scan_path, topdown=True):
                rel_dir = Path(dirpath).relative_to(scan_path)
                dirnames[:] = [
                    name
                    for name in dirnames
                    if not _matches_exclude(rel_dir / name, request.exclude_patterns)
                ]
                for filename in filenames:
                    if filename.endswith(".md"):
                        await self._append_document(
                            documents,
                            seen_files,
                            md_file=Path(dirpath) / filename,
                            scan_root=scan_path,
                            source_root=source_root,
                            exclude_patterns=request.exclude_patterns,
                        )

        return ModelIngestionResult(documents=documents)

    async def _append_document(
        self,
        documents: list[ModelDocumentEntry],
        seen_files: set[Path],
        *,
        md_file: Path,
        scan_root: Path,
        source_root: Path,
        exclude_patterns: list[str] | None,
    ) -> None:
        """Add one Markdown file once, preserving a readable canonical source path."""
        resolved_file = md_file.resolve()
        if resolved_file in seen_files:
            return
        try:
            rel_scan_path = resolved_file.relative_to(scan_root)
        except ValueError:
            logger.warning("File escaped ingestion scan root, skipping: %s", md_file)
            return
        if _matches_exclude(rel_scan_path, exclude_patterns):
            return
        seen_files.add(resolved_file)

        try:
            source_path = str(resolved_file.relative_to(source_root))
            sha256 = _compute_sha256(resolved_file)
            size = resolved_file.stat().st_size
            git_sha, author, created_at_raw, updated_at_raw = await self._git_metadata(
                resolved_file, scan_root
            )
            documents.append(
                ModelDocumentEntry(
                    source_path=source_path,
                    repo_name=source_root.name,
                    git_sha=git_sha,
                    author=author,
                    created_at=_parse_iso(created_at_raw),
                    updated_at=_parse_iso(updated_at_raw),
                    file_size_bytes=size,
                    source_content_sha256=sha256,
                )
            )
        except Exception:
            logger.exception("Failed to process file: %s", resolved_file)

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
    "_expand_root_paths",
    "_matches_exclude",
]
