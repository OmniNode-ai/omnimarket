# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain test for node_feature_dashboard_compute — zero infra."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from omnimarket.nodes.node_feature_dashboard_compute.handlers.handler_feature_dashboard_compute import (
    HandlerFeatureDashboardCompute,
)
from omnimarket.nodes.node_feature_dashboard_compute.models.model_feature_dashboard_request import (
    DEFAULT_CHECK_TYPES,
    ModelFeatureDashboardRequest,
)


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
        assert data["node_not_implemented"] is False
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
        from omnimarket.nodes.node_feature_dashboard_compute.handlers import (  # noqa: F401
            handler_feature_dashboard_compute,
        )

    def test_handler_class_exists(self) -> None:
        from omnimarket.nodes.node_feature_dashboard_compute.handlers.handler_feature_dashboard_compute import (
            HandlerFeatureDashboardCompute,
        )

        assert HandlerFeatureDashboardCompute is not None

    def test_input_model_exists(self) -> None:
        from omnimarket.nodes.node_feature_dashboard_compute.models.model_feature_dashboard_request import (
            ModelFeatureDashboardRequest,
        )

        assert ModelFeatureDashboardRequest is not None

    def test_output_model_exists(self) -> None:
        from omnimarket.nodes.node_feature_dashboard_compute.models.model_feature_dashboard_result import (
            ModelFeatureDashboardResult,
        )

        assert ModelFeatureDashboardResult is not None


class TestHandlerCompute:
    def test_handler_audits_registry_skill(self) -> None:
        """merge_sweep is dispatch-registered → discovered + non-zero coverage.

        Regression pin for the stale-discovery defect: the old SKILL.md
        directory scan reported 0% for skills that demonstrably work; the
        live-registry discovery must resolve merge_sweep's backing node.
        """
        handler = HandlerFeatureDashboardCompute()
        request = ModelFeatureDashboardRequest(skills=["merge_sweep"])

        result = handler.handle(request)

        assert result.status in {"complete", "partial"}
        assert result.skills_audited == 1
        assert result.checks_run == list(DEFAULT_CHECK_TYPES)
        assert "merge_sweep" in result.coverage_report
        coverage = cast(dict[str, Any], result.coverage_report["merge_sweep"])
        assert coverage["registered"] is True
        assert coverage["node_name"] == "node_pr_lifecycle_orchestrator"
        assert coverage["checks"]["backing_node"] is True
        assert coverage["checks"]["contract"] is True
        assert coverage["coverage_score"] > 0.0

    def test_handler_respects_check_filter(self) -> None:
        result = HandlerFeatureDashboardCompute().handle(
            ModelFeatureDashboardRequest(
                skills=["merge_sweep"],
                check_types=["backing_node", "contract"],
            )
        )

        coverage = result.coverage_report["merge_sweep"]
        assert isinstance(coverage, dict)
        assert set(coverage["checks"]) == {"backing_node", "contract"}
        assert result.checks_run == ["backing_node", "contract"]

    def test_handler_flags_unregistered_skill_filter(self) -> None:
        result = HandlerFeatureDashboardCompute().handle(
            ModelFeatureDashboardRequest(skills=["does-not-exist"])
        )

        assert result.status == "partial"
        assert result.skills_audited == 1
        coverage = cast(dict[str, Any], result.coverage_report["does-not-exist"])
        assert coverage["registered"] is False
        registry_gaps = [
            gap for gap in result.gaps if gap["check_type"] == "registry_entry"
        ]
        assert registry_gaps
        assert registry_gaps[0]["severity"] == "HIGH"

    def test_request_rejects_unknown_check_type(self) -> None:
        with pytest.raises(ValueError, match="unknown check_types"):
            ModelFeatureDashboardRequest(check_types=["nope"])
