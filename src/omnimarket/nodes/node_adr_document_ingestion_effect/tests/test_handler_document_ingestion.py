# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Tests for HandlerDocumentIngestion.

Unit tests use temp directories with synthetic .md files.
Integration tests (marked @pytest.mark.integration) run against the real docs/ tree.

[OMN-10693]
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from omnimarket.nodes.node_adr_document_ingestion_effect.handlers.handler_document_ingestion import (
    HandlerDocumentIngestion,
    _compute_sha256,
    _matches_exclude,
)
from omnimarket.nodes.node_adr_document_ingestion_effect.models.model_ingestion_request import (
    ModelIngestionRequest,
)
from omnimarket.nodes.node_adr_document_ingestion_effect.models.model_ingestion_result import (
    ModelIngestionResult,
)

# =============================================================================
# Helpers
# =============================================================================


def _write_md(path: Path, content: str = "# Test\n\nContent.") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


# =============================================================================
# Unit: _compute_sha256
# =============================================================================


@pytest.mark.unit
def test_compute_sha256_matches_hashlib(tmp_path: Path) -> None:
    f = tmp_path / "doc.md"
    content = "# Hello\n\nWorld."
    f.write_text(content, encoding="utf-8")
    raw = content.encode("utf-8")
    expected = hashlib.sha256(raw).hexdigest()
    assert _compute_sha256(f) == expected


@pytest.mark.unit
def test_compute_sha256_empty_file(tmp_path: Path) -> None:
    f = tmp_path / "empty.md"
    f.write_text("", encoding="utf-8")
    assert _compute_sha256(f) == hashlib.sha256(b"").hexdigest()


# =============================================================================
# Unit: _matches_exclude
# =============================================================================


@pytest.mark.unit
class TestMatchesExclude:
    def test_no_patterns_never_matches(self) -> None:
        assert not _matches_exclude(Path("a/b/c.md"), None)

    def test_node_modules_matches(self) -> None:
        assert _matches_exclude(Path("node_modules/foo/bar.md"), None)

    def test_git_dir_matches(self) -> None:
        assert _matches_exclude(Path(".git/COMMIT_EDITMSG"), None)

    def test_pycache_matches(self) -> None:
        assert _matches_exclude(Path("src/__pycache__/mod.cpython-312.pyc"), None)

    def test_omni_worktrees_matches(self) -> None:
        assert _matches_exclude(Path("omni_worktrees/OMN-123/repo/docs/adr.md"), None)

    def test_normal_path_does_not_match(self) -> None:
        assert not _matches_exclude(Path("docs/decisions/0001-use-kafka.md"), None)

    def test_custom_pattern_matches(self) -> None:
        assert _matches_exclude(Path("vendor/lib/readme.md"), ["vendor/"])

    def test_custom_pattern_no_match(self) -> None:
        assert not _matches_exclude(Path("src/foo/bar.md"), ["vendor/"])


# =============================================================================
# Unit: HandlerDocumentIngestion.handle — temp-dir based
# =============================================================================


@pytest.mark.unit
class TestHandlerDocumentIngestion:
    @pytest.mark.asyncio
    async def test_finds_md_files_in_root(self, tmp_path: Path) -> None:
        _write_md(tmp_path / "a.md")
        _write_md(tmp_path / "b.md")
        _write_md(tmp_path / "sub" / "c.md")

        mock_git = AsyncMock(
            return_value=(
                "abc123",
                "Alice",
                "2024-01-01T00:00:00+00:00",
                "2024-06-01T00:00:00+00:00",
            )
        )
        handler = HandlerDocumentIngestion()

        with patch.object(handler, "_git_metadata", mock_git):
            result = await handler.handle(
                request=ModelIngestionRequest(root_paths=[str(tmp_path)])
            )

        assert isinstance(result, ModelIngestionResult)
        assert len(result.documents) == 3
        paths = {d.source_path for d in result.documents}
        assert "a.md" in paths
        assert "b.md" in paths
        assert str(Path("sub") / "c.md") in paths

    @pytest.mark.asyncio
    async def test_source_path_is_relative_to_root(self, tmp_path: Path) -> None:
        _write_md(tmp_path / "deep" / "nested" / "doc.md")
        mock_git = AsyncMock(return_value=(None, None, None, None))
        handler = HandlerDocumentIngestion()

        with patch.object(handler, "_git_metadata", mock_git):
            result = await handler.handle(
                request=ModelIngestionRequest(root_paths=[str(tmp_path)])
            )

        assert len(result.documents) == 1
        assert result.documents[0].source_path == str(
            Path("deep") / "nested" / "doc.md"
        )

    @pytest.mark.asyncio
    async def test_sha256_computed_correctly(self, tmp_path: Path) -> None:
        content = "# ADR-001\n\nDecision: use Kafka."
        _write_md(tmp_path / "adr.md", content)
        expected_sha = hashlib.sha256(content.encode("utf-8")).hexdigest()

        mock_git = AsyncMock(return_value=(None, None, None, None))
        handler = HandlerDocumentIngestion()

        with patch.object(handler, "_git_metadata", mock_git):
            result = await handler.handle(
                request=ModelIngestionRequest(root_paths=[str(tmp_path)])
            )

        assert result.documents[0].source_content_sha256 == expected_sha

    @pytest.mark.asyncio
    async def test_file_size_bytes_correct(self, tmp_path: Path) -> None:
        content = "# Size Test"
        f = _write_md(tmp_path / "size.md", content)
        expected_size = f.stat().st_size

        mock_git = AsyncMock(return_value=(None, None, None, None))
        handler = HandlerDocumentIngestion()

        with patch.object(handler, "_git_metadata", mock_git):
            result = await handler.handle(
                request=ModelIngestionRequest(root_paths=[str(tmp_path)])
            )

        assert result.documents[0].file_size_bytes == expected_size

    @pytest.mark.asyncio
    async def test_excludes_node_modules(self, tmp_path: Path) -> None:
        _write_md(tmp_path / "keep.md")
        _write_md(tmp_path / "node_modules" / "pkg" / "readme.md")

        mock_git = AsyncMock(return_value=(None, None, None, None))
        handler = HandlerDocumentIngestion()

        with patch.object(handler, "_git_metadata", mock_git):
            result = await handler.handle(
                request=ModelIngestionRequest(root_paths=[str(tmp_path)])
            )

        assert len(result.documents) == 1
        assert result.documents[0].source_path == "keep.md"

    @pytest.mark.asyncio
    async def test_excludes_git_dir(self, tmp_path: Path) -> None:
        _write_md(tmp_path / "doc.md")
        _write_md(tmp_path / ".git" / "COMMIT_EDITMSG")

        mock_git = AsyncMock(return_value=(None, None, None, None))
        handler = HandlerDocumentIngestion()

        with patch.object(handler, "_git_metadata", mock_git):
            result = await handler.handle(
                request=ModelIngestionRequest(root_paths=[str(tmp_path)])
            )

        assert len(result.documents) == 1

    @pytest.mark.asyncio
    async def test_excludes_pycache(self, tmp_path: Path) -> None:
        _write_md(tmp_path / "real.md")
        _write_md(tmp_path / "__pycache__" / "mod.md")

        mock_git = AsyncMock(return_value=(None, None, None, None))
        handler = HandlerDocumentIngestion()

        with patch.object(handler, "_git_metadata", mock_git):
            result = await handler.handle(
                request=ModelIngestionRequest(root_paths=[str(tmp_path)])
            )

        assert len(result.documents) == 1

    @pytest.mark.asyncio
    async def test_custom_exclude_pattern(self, tmp_path: Path) -> None:
        _write_md(tmp_path / "keep.md")
        _write_md(tmp_path / "vendor" / "lib.md")

        mock_git = AsyncMock(return_value=(None, None, None, None))
        handler = HandlerDocumentIngestion()

        with patch.object(handler, "_git_metadata", mock_git):
            result = await handler.handle(
                request=ModelIngestionRequest(
                    root_paths=[str(tmp_path)], exclude_patterns=["vendor/"]
                )
            )

        assert len(result.documents) == 1
        assert result.documents[0].source_path == "keep.md"

    @pytest.mark.asyncio
    async def test_git_metadata_populated(self, tmp_path: Path) -> None:
        _write_md(tmp_path / "adr.md")
        mock_git = AsyncMock(
            return_value=(
                "deadbeef",
                "Bob",
                "2024-01-01T10:00:00+00:00",
                "2024-03-01T12:00:00+00:00",
            )
        )
        handler = HandlerDocumentIngestion()

        with patch.object(handler, "_git_metadata", mock_git):
            result = await handler.handle(
                request=ModelIngestionRequest(root_paths=[str(tmp_path)])
            )

        doc = result.documents[0]
        assert doc.git_sha == "deadbeef"
        assert doc.author == "Bob"
        assert doc.created_at is not None
        assert doc.updated_at is not None

    @pytest.mark.asyncio
    async def test_git_metadata_none_when_unavailable(self, tmp_path: Path) -> None:
        _write_md(tmp_path / "adr.md")
        mock_git = AsyncMock(return_value=(None, None, None, None))
        handler = HandlerDocumentIngestion()

        with patch.object(handler, "_git_metadata", mock_git):
            result = await handler.handle(
                request=ModelIngestionRequest(root_paths=[str(tmp_path)])
            )

        doc = result.documents[0]
        assert doc.git_sha is None
        assert doc.author is None
        assert doc.created_at is None
        assert doc.updated_at is None

    @pytest.mark.asyncio
    async def test_multiple_root_paths(self, tmp_path: Path) -> None:
        root_a = tmp_path / "repoA"
        root_b = tmp_path / "repoB"
        _write_md(root_a / "doc1.md")
        _write_md(root_b / "doc2.md")

        mock_git = AsyncMock(return_value=(None, None, None, None))
        handler = HandlerDocumentIngestion()

        with patch.object(handler, "_git_metadata", mock_git):
            result = await handler.handle(
                request=ModelIngestionRequest(root_paths=[str(root_a), str(root_b)])
            )

        assert len(result.documents) == 2

    @pytest.mark.asyncio
    async def test_repo_name_derived_from_root_path(self, tmp_path: Path) -> None:
        root = tmp_path / "omnibase_core"
        _write_md(root / "adr.md")

        mock_git = AsyncMock(return_value=(None, None, None, None))
        handler = HandlerDocumentIngestion()

        with patch.object(handler, "_git_metadata", mock_git):
            result = await handler.handle(
                request=ModelIngestionRequest(root_paths=[str(root)])
            )

        assert result.documents[0].repo_name == "omnibase_core"

    @pytest.mark.asyncio
    async def test_empty_root_returns_empty_result(self, tmp_path: Path) -> None:
        mock_git = AsyncMock(return_value=(None, None, None, None))
        handler = HandlerDocumentIngestion()

        with patch.object(handler, "_git_metadata", mock_git):
            result = await handler.handle(
                request=ModelIngestionRequest(root_paths=[str(tmp_path)])
            )

        assert result.documents == []

    @pytest.mark.asyncio
    async def test_file_root_path_skipped(self, tmp_path: Path) -> None:
        file_path = tmp_path / "not_a_dir.md"
        file_path.write_text("# content", encoding="utf-8")
        valid_root = tmp_path / "valid_dir"
        _write_md(valid_root / "real.md")

        mock_git = AsyncMock(return_value=(None, None, None, None))
        handler = HandlerDocumentIngestion()

        with patch.object(handler, "_git_metadata", mock_git):
            result = await handler.handle(
                request=ModelIngestionRequest(
                    root_paths=[str(file_path), str(valid_root)]
                )
            )

        assert len(result.documents) == 1
        assert result.documents[0].source_path == "real.md"

    @pytest.mark.asyncio
    async def test_nonexistent_root_path_skipped(self, tmp_path: Path) -> None:
        missing = str(tmp_path / "does_not_exist")
        _write_md(tmp_path / "real.md")

        mock_git = AsyncMock(return_value=(None, None, None, None))
        handler = HandlerDocumentIngestion()

        with patch.object(handler, "_git_metadata", mock_git):
            result = await handler.handle(
                request=ModelIngestionRequest(root_paths=[missing, str(tmp_path)])
            )

        assert len(result.documents) == 1

    @pytest.mark.asyncio
    async def test_only_md_files_collected(self, tmp_path: Path) -> None:
        _write_md(tmp_path / "doc.md")
        (tmp_path / "script.py").write_text("pass\n", encoding="utf-8")
        (tmp_path / "data.json").write_text("{}\n", encoding="utf-8")

        mock_git = AsyncMock(return_value=(None, None, None, None))
        handler = HandlerDocumentIngestion()

        with patch.object(handler, "_git_metadata", mock_git):
            result = await handler.handle(
                request=ModelIngestionRequest(root_paths=[str(tmp_path)])
            )

        assert len(result.documents) == 1
        assert result.documents[0].source_path == "doc.md"


# =============================================================================
# Unit: _git_metadata subprocess parsing
# =============================================================================


@pytest.mark.unit
class TestGitMetadataSubprocess:
    @pytest.mark.asyncio
    async def test_parses_valid_git_log_output(self, tmp_path: Path) -> None:
        _write_md(tmp_path / "adr.md")
        handler = HandlerDocumentIngestion()

        fake_stdout = "abc1234|Jane Doe|2024-02-15T09:30:00+00:00"

        with patch(
            "omnimarket.nodes.node_adr_document_ingestion_effect.handlers.handler_document_ingestion.asyncio.create_subprocess_exec",
        ) as mock_proc_cls:
            mock_proc = AsyncMock()
            mock_proc.communicate = AsyncMock(return_value=(fake_stdout.encode(), b""))
            mock_proc.returncode = 0
            mock_proc_cls.return_value = mock_proc

            git_sha, author, _created_at, updated_at = await handler._git_metadata(  # noqa: SLF001
                tmp_path / "adr.md", tmp_path
            )

        assert git_sha == "abc1234"
        assert author == "Jane Doe"
        assert updated_at is not None

    @pytest.mark.asyncio
    async def test_returns_none_tuple_on_git_failure(self, tmp_path: Path) -> None:
        _write_md(tmp_path / "adr.md")
        handler = HandlerDocumentIngestion()

        with patch(
            "omnimarket.nodes.node_adr_document_ingestion_effect.handlers.handler_document_ingestion.asyncio.create_subprocess_exec",
        ) as mock_proc_cls:
            mock_proc = AsyncMock()
            mock_proc.communicate = AsyncMock(
                return_value=(b"", b"fatal: not a git repo")
            )
            mock_proc.returncode = 128
            mock_proc_cls.return_value = mock_proc

            git_sha, author, created_at, updated_at = await handler._git_metadata(  # noqa: SLF001
                tmp_path / "adr.md", tmp_path
            )

        assert git_sha is None
        assert author is None
        assert created_at is None
        assert updated_at is None

    @pytest.mark.asyncio
    async def test_returns_none_tuple_on_exception(self, tmp_path: Path) -> None:
        _write_md(tmp_path / "adr.md")
        handler = HandlerDocumentIngestion()

        with patch(
            "omnimarket.nodes.node_adr_document_ingestion_effect.handlers.handler_document_ingestion.asyncio.create_subprocess_exec",
            side_effect=FileNotFoundError("git not found"),
        ):
            git_sha, author, _created_at, _updated_at = await handler._git_metadata(  # noqa: SLF001
                tmp_path / "adr.md", tmp_path
            )

        assert git_sha is None
        assert author is None


# =============================================================================
# Integration — real docs/ tree (requires omni_home checkout)
# =============================================================================


@pytest.mark.integration
@pytest.mark.asyncio
async def test_integration_real_docs_tree() -> None:
    omni_home = os.environ.get("OMNI_HOME")
    if not omni_home:
        pytest.skip("OMNI_HOME not set — skipping integration probe")

    docs_path = Path(omni_home) / "omni_home" / "docs"
    if not docs_path.exists():
        pytest.skip(f"docs path not found: {docs_path}")

    handler = HandlerDocumentIngestion()
    result = await handler.handle(
        request=ModelIngestionRequest(root_paths=[str(docs_path)])
    )

    assert isinstance(result, ModelIngestionResult)
    assert len(result.documents) > 0
    for doc in result.documents:
        assert doc.source_path
        assert doc.source_content_sha256
        assert len(doc.source_content_sha256) == 64
        assert doc.file_size_bytes >= 0
