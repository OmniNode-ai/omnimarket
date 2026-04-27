# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for HandlerCrawlFilesystem — OMN-7875.

Uses temp directories with .py, .md, .yaml, contract.yaml files.
Verifies event count, content_type assignments, hash presence, exclusion, and file-list mode.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from omnimarket.nodes.node_filesystem_crawler_effect.handlers.handler_crawl_filesystem import (
    HandlerCrawlFilesystem,
    detect_content_type,
)
from omnimarket.nodes.node_filesystem_crawler_effect.models.model_crawl_filesystem_request import (
    ModelCrawlFilesystemRequest,
)


@pytest.fixture
def handler() -> HandlerCrawlFilesystem:
    return HandlerCrawlFilesystem(max_files=1000)


@pytest.fixture
def tmp_tree(tmp_path: Path) -> Path:
    """Create a temp directory tree with mixed file types."""
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "main.py").write_text('print("hello")')
    (tmp_path / "app" / "__init__.py").write_text("")

    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "readme.md").write_text("# Hello")
    (tmp_path / "docs" / "design.md").write_text("## Design")

    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "settings.yaml").write_text("key: value")
    (tmp_path / "config" / "contract.yaml").write_text("name: test")

    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "cached.pyc").write_text("junk")

    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text("ref: refs/heads/main")

    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "pkg").mkdir()
    (tmp_path / "node_modules" / "pkg" / "index.js").write_text("module.exports = {}")

    (tmp_path / "data.json").write_text('{"a": 1}')
    (tmp_path / "top.py").write_text("x = 1")

    return tmp_path


class TestDetectContentType:
    def test_python(self) -> None:
        assert detect_content_type(Path("foo.py")) == "python"

    def test_typescript(self) -> None:
        assert detect_content_type(Path("foo.ts")) == "typescript"
        assert detect_content_type(Path("foo.tsx")) == "typescript"

    def test_markdown(self) -> None:
        assert detect_content_type(Path("foo.md")) == "markdown"

    def test_yaml(self) -> None:
        assert detect_content_type(Path("foo.yaml")) == "yaml"
        assert detect_content_type(Path("foo.yml")) == "yaml"

    def test_contract_yaml(self) -> None:
        assert detect_content_type(Path("contract.yaml")) == "contract"
        assert detect_content_type(Path("subdir/contract.yaml")) == "contract"

    def test_unknown(self) -> None:
        assert detect_content_type(Path("foo.txt")) == "unknown"
        assert detect_content_type(Path("Makefile")) == "unknown"

    def test_json(self) -> None:
        assert detect_content_type(Path("data.json")) == "json"


@pytest.mark.asyncio
class TestDirectoryWalkMode:
    async def test_walk_emits_events_for_all_file_types(
        self, handler: HandlerCrawlFilesystem, tmp_tree: Path
    ) -> None:
        request = ModelCrawlFilesystemRequest(root_paths=[str(tmp_tree)])
        result = await handler.crawl(request, correlation_id=uuid4())

        events = result.events
        content_types = {e.content_type for e in events}
        source_refs = {e.source_ref for e in events}

        assert "python" in content_types
        assert "markdown" in content_types
        assert "yaml" in content_types
        assert "contract" in content_types
        assert "json" in content_types

        for f in [
            tmp_tree / "app" / "main.py",
            tmp_tree / "app" / "__init__.py",
            tmp_tree / "top.py",
            tmp_tree / "docs" / "readme.md",
            tmp_tree / "config" / "settings.yaml",
            tmp_tree / "config" / "contract.yaml",
            tmp_tree / "data.json",
        ]:
            assert str(f) in source_refs, f"Expected {f} in events"

    async def test_excludes_standard_junk_dirs(
        self, handler: HandlerCrawlFilesystem, tmp_tree: Path
    ) -> None:
        request = ModelCrawlFilesystemRequest(root_paths=[str(tmp_tree)])
        result = await handler.crawl(request, correlation_id=uuid4())

        source_refs = {e.source_ref for e in result.events}

        assert not any("__pycache__" in r for r in source_refs)
        assert not any(".git" in r for r in source_refs)
        assert not any("node_modules" in r for r in source_refs)

    async def test_contract_yaml_maps_to_contract_type(
        self, handler: HandlerCrawlFilesystem, tmp_tree: Path
    ) -> None:
        request = ModelCrawlFilesystemRequest(root_paths=[str(tmp_tree)])
        result = await handler.crawl(request, correlation_id=uuid4())

        contract_events = [e for e in result.events if e.content_type == "contract"]
        assert len(contract_events) == 1
        assert contract_events[0].source_ref.endswith("contract.yaml")

    async def test_yaml_files_map_to_yaml_not_contract(
        self, handler: HandlerCrawlFilesystem, tmp_tree: Path
    ) -> None:
        request = ModelCrawlFilesystemRequest(root_paths=[str(tmp_tree)])
        result = await handler.crawl(request, correlation_id=uuid4())

        yaml_events = [e for e in result.events if e.content_type == "yaml"]
        assert len(yaml_events) == 1
        assert "settings.yaml" in yaml_events[0].source_ref

    async def test_events_have_sha256_fingerprint(
        self, handler: HandlerCrawlFilesystem, tmp_tree: Path
    ) -> None:
        request = ModelCrawlFilesystemRequest(root_paths=[str(tmp_tree)])
        result = await handler.crawl(request, correlation_id=uuid4())

        for event in result.events:
            assert len(event.content_fingerprint) == 64
            assert all(c in "0123456789abcdef" for c in event.content_fingerprint)

    async def test_events_have_mtime_and_size(
        self, handler: HandlerCrawlFilesystem, tmp_tree: Path
    ) -> None:
        request = ModelCrawlFilesystemRequest(root_paths=[str(tmp_tree)])
        result = await handler.crawl(request, correlation_id=uuid4())

        for event in result.events:
            assert event.mtime > 0
            assert event.file_size_bytes >= 0

    async def test_events_have_relative_path(
        self, handler: HandlerCrawlFilesystem, tmp_tree: Path
    ) -> None:
        request = ModelCrawlFilesystemRequest(root_paths=[str(tmp_tree)])
        result = await handler.crawl(request, correlation_id=uuid4())

        rel_paths = {e.relative_path for e in result.events}
        assert "top.py" in rel_paths
        assert str(Path("app") / "main.py") in rel_paths

    async def test_extension_filter(
        self, handler: HandlerCrawlFilesystem, tmp_tree: Path
    ) -> None:
        request = ModelCrawlFilesystemRequest(
            root_paths=[str(tmp_tree)],
            extensions=[".py"],
        )
        result = await handler.crawl(request, correlation_id=uuid4())

        assert all(e.content_type == "python" for e in result.events)

    async def test_custom_exclude_dirs(
        self, handler: HandlerCrawlFilesystem, tmp_tree: Path
    ) -> None:
        request = ModelCrawlFilesystemRequest(
            root_paths=[str(tmp_tree)],
            exclude_dirs=["__pycache__", ".git", "node_modules", ".venv", "docs"],
        )
        result = await handler.crawl(request, correlation_id=uuid4())

        source_refs = {e.source_ref for e in result.events}
        assert not any("docs" in r for r in source_refs)

    async def test_nonexistent_root_path_skipped(
        self, handler: HandlerCrawlFilesystem, tmp_path: Path
    ) -> None:
        fake = str(tmp_path / "does_not_exist")
        request = ModelCrawlFilesystemRequest(root_paths=[fake])
        result = await handler.crawl(request, correlation_id=uuid4())

        assert result.files_scanned == 0
        assert len(result.events) == 0

    async def test_max_files_cap(self, tmp_tree: Path) -> None:
        small_handler = HandlerCrawlFilesystem(max_files=2)
        request = ModelCrawlFilesystemRequest(root_paths=[str(tmp_tree)])
        result = await small_handler.crawl(request, correlation_id=uuid4())

        assert result.truncated is True
        assert result.files_scanned <= 2

    async def test_relative_path_not_omitting_non_py(
        self, handler: HandlerCrawlFilesystem, tmp_tree: Path
    ) -> None:
        request = ModelCrawlFilesystemRequest(root_paths=[str(tmp_tree)])
        result = await handler.crawl(request, correlation_id=uuid4())

        rel_paths = {e.relative_path for e in result.events}
        assert str(Path("config") / "settings.yaml") in rel_paths
        assert str(Path("config") / "contract.yaml") in rel_paths


@pytest.mark.asyncio
class TestFileListMode:
    async def test_file_list_mode(
        self, handler: HandlerCrawlFilesystem, tmp_tree: Path
    ) -> None:
        files = [
            str(tmp_tree / "app" / "main.py"),
            str(tmp_tree / "docs" / "readme.md"),
            str(tmp_tree / "config" / "contract.yaml"),
        ]
        request = ModelCrawlFilesystemRequest(file_list=files)
        result = await handler.crawl(request, correlation_id=uuid4())

        assert result.files_scanned == 3
        assert len(result.events) == 3

        types = {e.content_type for e in result.events}
        assert types == {"python", "markdown", "contract"}

    async def test_file_list_nonexistent_skipped(
        self, handler: HandlerCrawlFilesystem, tmp_tree: Path
    ) -> None:
        files = [
            str(tmp_tree / "app" / "main.py"),
            str(tmp_tree / "nope.py"),
        ]
        request = ModelCrawlFilesystemRequest(file_list=files)
        result = await handler.crawl(request, correlation_id=uuid4())

        assert result.error_count == 1
        assert result.files_scanned == 1

    async def test_file_list_excludes_match(
        self, handler: HandlerCrawlFilesystem, tmp_tree: Path
    ) -> None:
        files = [
            str(tmp_tree / "app" / "main.py"),
            str(tmp_tree / "__pycache__" / "cached.pyc"),
        ]
        request = ModelCrawlFilesystemRequest(file_list=files)
        result = await handler.crawl(request, correlation_id=uuid4())

        assert result.files_scanned == 1
        assert result.skipped_count == 1


@pytest.mark.asyncio
class TestAbsolutePathValidation:
    async def test_rejects_relative_root_path(self) -> None:
        with pytest.raises(ValueError, match="absolute"):
            ModelCrawlFilesystemRequest(root_paths=["relative/path"])

    async def test_accepts_absolute_root_path(self) -> None:
        req = ModelCrawlFilesystemRequest(root_paths=["/tmp"])
        assert req.root_paths == ["/tmp"]


@pytest.mark.asyncio
class TestNoOmniHomeDependency:
    async def test_no_env_var_read(
        self, handler: HandlerCrawlFilesystem, tmp_tree: Path
    ) -> None:
        import os

        os.environ.pop("OMNI_HOME", None)
        request = ModelCrawlFilesystemRequest(root_paths=[str(tmp_tree)])
        result = await handler.crawl(request, correlation_id=uuid4())

        assert result.files_scanned > 0
        assert len(result.events) > 0
