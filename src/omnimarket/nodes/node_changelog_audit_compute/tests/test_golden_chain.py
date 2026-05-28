# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain test for node_changelog_audit_compute — zero infra."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml


@pytest.fixture
def node_dir() -> Path:
    return Path(__file__).resolve().parent.parent


@pytest.fixture
def contract_path(node_dir: Path) -> Path:
    return node_dir / "contract.yaml"


@pytest.fixture
def metadata_path(node_dir: Path) -> Path:
    return node_dir / "metadata.yaml"


class TestContractYaml:
    def test_contract_exists(self, contract_path: Path) -> None:
        assert contract_path.exists()

    def test_contract_loads(self, contract_path: Path) -> None:
        with open(contract_path) as f:
            data = yaml.safe_load(f)
        assert isinstance(data, dict)
        assert "name" in data
        assert "handler" in data

    def test_contract_declares_handler(self, contract_path: Path) -> None:
        with open(contract_path) as f:
            data = yaml.safe_load(f)
        handler = data.get("handler", {})
        assert "module" in handler
        assert "class" in handler


class TestMetadataYaml:
    def test_metadata_exists(self, metadata_path: Path) -> None:
        assert metadata_path.exists()

    def test_metadata_loads(self, metadata_path: Path) -> None:
        with open(metadata_path) as f:
            data = yaml.safe_load(f)
        assert isinstance(data, dict)
        assert "name" in data
        assert "version" in data
        assert "entry_points" in data


class TestHandlerImport:
    def test_handler_module_imports(self) -> None:
        from omnimarket.nodes.node_changelog_audit_compute.handlers import (  # noqa: F401
            handler_changelog_audit_compute,
        )

    def test_handler_class_exists(self) -> None:
        from omnimarket.nodes.node_changelog_audit_compute.handlers.handler_changelog_audit_compute import (
            HandlerChangelogAuditCompute,
        )

        assert HandlerChangelogAuditCompute is not None

    def test_input_model_exists(self) -> None:
        from omnimarket.nodes.node_changelog_audit_compute.models.model_changelog_audit_request import (
            ModelChangelogAuditRequest,
        )

        assert ModelChangelogAuditRequest is not None

    def test_output_model_exists(self) -> None:
        from omnimarket.nodes.node_changelog_audit_compute.models.model_changelog_audit_result import (
            ModelChangelogAuditResult,
        )

        assert ModelChangelogAuditResult is not None


class TestHandler:
    def test_handler_classifies_supplied_changelog(self) -> None:
        from omnimarket.nodes.node_changelog_audit_compute.handlers.handler_changelog_audit_compute import (
            HandlerChangelogAuditCompute,
        )
        from omnimarket.nodes.node_changelog_audit_compute.models.model_changelog_audit_request import (
            ModelChangelogAuditRequest,
        )

        handler = HandlerChangelogAuditCompute()
        request = ModelChangelogAuditRequest(
            repos=["omnimarket"],
            since_date="2026-01-01",
            dependencies=["pydantic"],
            changelog_contents={
                "omnimarket": """
## [1.2.0] - 2026-02-10
### Breaking Changes
- breaking: update pydantic model config contract
### Fixed
- fix: repair unrelated kafka consumer

## [1.1.0] - 2025-12-31
### Added
- feat: add old pydantic feature
""".strip()
            },
        )
        result = handler.handle(request)

        assert len(result.entries) == 1
        assert result.entries[0].entry_type == "breaking"
        assert result.entries[0].affects_dependencies == ["pydantic"]
        assert result.summary == {
            "breaking": 1,
            "feature": 0,
            "fix": 0,
            "chore": 0,
            "unknown": 0,
        }

    def test_handler_returns_empty_summary_for_missing_content(self) -> None:
        from omnimarket.nodes.node_changelog_audit_compute.handlers.handler_changelog_audit_compute import (
            HandlerChangelogAuditCompute,
        )
        from omnimarket.nodes.node_changelog_audit_compute.models.model_changelog_audit_request import (
            ModelChangelogAuditRequest,
        )

        handler = HandlerChangelogAuditCompute()
        result = handler.handle(
            ModelChangelogAuditRequest(repos=["omnimarket"], since_date="2026-01-01")
        )

        assert result.entries == []
        assert result.summary == {
            "breaking": 0,
            "feature": 0,
            "fix": 0,
            "chore": 0,
            "unknown": 0,
        }
