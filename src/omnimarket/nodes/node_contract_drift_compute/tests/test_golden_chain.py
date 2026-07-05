# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Golden chain test for node_contract_drift_compute.

Verifies OMN-12346: the contract_drift_compute node is implemented as a native
deterministic compute handler. The contract is the source of truth for runtime
event-bus topics, and handler-only topic literals are reported as drift.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest
import yaml

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def node_dir() -> Path:
    """Path to the node_contract_drift_compute directory."""
    return Path(__file__).resolve().parent.parent


@pytest.fixture
def contract_path(node_dir: Path) -> Path:
    return node_dir / "contract.yaml"


@pytest.fixture
def metadata_path(node_dir: Path) -> Path:
    return node_dir / "metadata.yaml"


def _write_native_node(
    omni_home: Path,
    *,
    node_name: str = "node_native_sample",
    handler_body: str,
    publish_topics: list[str] | None = None,
) -> Path:
    repo = omni_home / "omnimarket"
    node_dir = repo / "src" / "omnimarket" / "nodes" / node_name
    handlers_dir = node_dir / "handlers"
    handlers_dir.mkdir(parents=True)
    (repo / "src").mkdir(parents=True, exist_ok=True)
    (handlers_dir / "__init__.py").write_text("")
    (handlers_dir / "handler_native_sample.py").write_text(dedent(handler_body))
    contract = {
        "name": node_name,
        "node_type": "compute",
        "node_version": {"major": 1, "minor": 0, "patch": 0},
        "contract_version": {"major": 1, "minor": 0, "patch": 0},
        "description": "Native sample node",
        "handler": {
            "module": "omnimarket.nodes.node_native_sample.handlers.handler_native_sample",
            "class": "NodeNativeSample",
            "input_model": "omnimarket.nodes.node_native_sample.models.ModelRequest",
        },
        "terminal_event": "onex.evt.omnimarket.native-sample-completed.v1",
        "event_bus": {
            "subscribe_topics": ["onex.cmd.omnimarket.native-sample-start.v1"],
            "publish_topics": publish_topics
            if publish_topics is not None
            else ["onex.evt.omnimarket.native-sample-completed.v1"],
        },
    }
    (node_dir / "contract.yaml").write_text(yaml.safe_dump(contract, sort_keys=False))
    return node_dir


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestContractYaml:
    """Contract YAML is valid and declares required fields."""

    def test_contract_exists(self, contract_path: Path) -> None:
        assert contract_path.exists(), f"contract.yaml not found at {contract_path}"

    def test_contract_loads(self, contract_path: Path) -> None:
        with open(contract_path) as f:
            data = yaml.safe_load(f)
        assert isinstance(data, dict)
        assert "name" in data
        assert "contract_version" in data
        assert "handler" in data

    def test_contract_node_type_is_compute(self, contract_path: Path) -> None:
        with open(contract_path) as f:
            data = yaml.safe_load(f)
        assert data.get("node_type") == "compute"

    def test_contract_is_marked_implemented(self, contract_path: Path) -> None:
        with open(contract_path) as f:
            data = yaml.safe_load(f)
        assert data.get("node_not_implemented") is False

    def test_contract_purity_is_pure(self, contract_path: Path) -> None:
        with open(contract_path) as f:
            data = yaml.safe_load(f)
        descriptor = data.get("descriptor", {})
        assert descriptor.get("purity") == "pure"

    def test_contract_idempotent(self, contract_path: Path) -> None:
        with open(contract_path) as f:
            data = yaml.safe_load(f)
        descriptor = data.get("descriptor", {})
        assert descriptor.get("idempotent") is True

    def test_contract_declares_handler(self, contract_path: Path) -> None:
        with open(contract_path) as f:
            data = yaml.safe_load(f)
        handler = data.get("handler", {})
        assert "module" in handler, "handler.module not declared"
        assert "class" in handler, "handler.class not declared"
        assert "input_model" in handler, "handler.input_model not declared"

    def test_contract_declares_event_bus(self, contract_path: Path) -> None:
        with open(contract_path) as f:
            data = yaml.safe_load(f)
        event_bus = data.get("event_bus", {})
        assert "subscribe_topics" in event_bus
        assert "publish_topics" in event_bus
        assert len(event_bus["subscribe_topics"]) > 0
        assert len(event_bus["publish_topics"]) > 0

    def test_contract_topics_follow_naming_convention(
        self, contract_path: Path
    ) -> None:
        with open(contract_path) as f:
            data = yaml.safe_load(f)
        event_bus = data.get("event_bus", {})
        all_topics = event_bus.get("subscribe_topics", []) + event_bus.get(
            "publish_topics", []
        )
        for topic in all_topics:
            assert topic.startswith("onex."), (
                f"Topic {topic!r} does not start with 'onex.'"
            )


class TestMetadataYaml:
    """Metadata YAML is valid and has required fields."""

    def test_metadata_exists(self, metadata_path: Path) -> None:
        assert metadata_path.exists(), f"metadata.yaml not found at {metadata_path}"

    def test_metadata_loads(self, metadata_path: Path) -> None:
        with open(metadata_path) as f:
            data = yaml.safe_load(f)
        assert isinstance(data, dict)
        assert "name" in data
        assert "version" in data
        assert "entry_points" in data

    def test_metadata_node_role_is_compute(self, metadata_path: Path) -> None:
        with open(metadata_path) as f:
            data = yaml.safe_load(f)
        assert data.get("node_role") == "compute"


class TestHandlerImport:
    """Handler module can be imported and class/models exist."""

    def test_handler_module_imports(self) -> None:
        from omnimarket.nodes.node_contract_drift_compute.handlers import (
            handler_contract_drift_compute,
        )

        assert handler_contract_drift_compute is not None

    def test_handler_class_exists(self) -> None:
        from omnimarket.nodes.node_contract_drift_compute.handlers.handler_contract_drift_compute import (
            NodeContractDriftCompute,
        )

        assert NodeContractDriftCompute is not None

    def test_input_model_exists(self) -> None:
        from omnimarket.nodes.node_contract_drift_compute.handlers.handler_contract_drift_compute import (
            ModelContractDriftComputeRequest,
        )

        assert ModelContractDriftComputeRequest is not None

    def test_output_model_exists(self) -> None:
        from omnimarket.nodes.node_contract_drift_compute.handlers.handler_contract_drift_compute import (
            ModelContractDriftComputeResult,
        )

        assert ModelContractDriftComputeResult is not None

    def test_finding_model_exists(self) -> None:
        from omnimarket.nodes.node_contract_drift_compute.handlers.handler_contract_drift_compute import (
            ModelContractDriftFinding,
        )

        assert ModelContractDriftFinding is not None

    def test_boundary_finding_model_exists(self) -> None:
        from omnimarket.nodes.node_contract_drift_compute.handlers.handler_contract_drift_compute import (
            ModelBoundaryFinding,
        )

        assert ModelBoundaryFinding is not None


class TestHandlerBehaviour:
    """Handler performs deterministic contract-vs-handler topic drift detection."""

    def test_handler_returns_clean_result_for_contract_declared_topics(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from omnimarket.nodes.node_contract_drift_compute.handlers.handler_contract_drift_compute import (
            ModelContractDriftComputeRequest,
            NodeContractDriftCompute,
        )

        _write_native_node(
            tmp_path,
            handler_body="""
            TOPIC_START = "onex.cmd.omnimarket.native-sample-start.v1"
            TOPIC_DONE = "onex.evt.omnimarket.native-sample-completed.v1"
            """,
        )
        monkeypatch.setenv("OMNI_HOME", str(tmp_path))

        handler = NodeContractDriftCompute()
        request = ModelContractDriftComputeRequest(
            repos=["omnimarket"], check_boundaries=False
        )
        result = handler.handle(request)

        assert result.overall_status == "clean"
        assert result.repos_scanned == 1
        assert result.total_contracts_checked == 1
        assert result.drifted_contracts == []
        assert result.violations == []
        assert result.staleness_scores == {"omnimarket": 0.0}

    def test_handler_reports_handler_only_topic_as_breaking_drift(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from omnimarket.nodes.node_contract_drift_compute.handlers.handler_contract_drift_compute import (
            ModelContractDriftComputeRequest,
            NodeContractDriftCompute,
        )

        _write_native_node(
            tmp_path,
            handler_body="""
            TOPIC_START = "onex.cmd.omnimarket.native-sample-start.v1"
            TOPIC_DONE = "onex.evt.omnimarket.native-sample-completed.v1"
            TOPIC_DRIFT = "onex.evt.omnimarket.undeclared-drift.v1"
            """,
        )
        monkeypatch.setenv("OMNI_HOME", str(tmp_path))

        result = NodeContractDriftCompute().handle(
            ModelContractDriftComputeRequest(
                repos=["omnimarket"], check_boundaries=False
            )
        )

        assert result.overall_status == "breaking"
        assert result.repos_scanned == 1
        assert result.total_contracts_checked == 1
        assert result.staleness_scores == {"omnimarket": 1.0}
        assert len(result.drifted_contracts) == 1
        finding = result.drifted_contracts[0]
        assert finding.repo == "omnimarket"
        assert finding.path.endswith("/contract.yaml")
        assert finding.severity == "BREAKING"
        assert "handler topic literal" in finding.summary
        assert any(
            change.path
            == "handlers.topic_literals.onex.evt.omnimarket.undeclared-drift.v1"
            and change.is_breaking
            and change.severity == "BREAKING"
            for change in finding.field_changes
        )
        assert result.violations == [
            f"omnimarket:{finding.path}: BREAKING {finding.summary}"
        ]

    def test_strict_mode_reports_contract_topic_not_present_as_handler_literal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from omnimarket.nodes.node_contract_drift_compute.handlers.handler_contract_drift_compute import (
            ModelContractDriftComputeRequest,
            NodeContractDriftCompute,
        )

        _write_native_node(
            tmp_path,
            handler_body='TOPIC_START = "onex.cmd.omnimarket.native-sample-start.v1"\n',
        )
        monkeypatch.setenv("OMNI_HOME", str(tmp_path))

        result = NodeContractDriftCompute().handle(
            ModelContractDriftComputeRequest(
                repos=["omnimarket"],
                check_boundaries=False,
                sensitivity="STRICT",
                severity_threshold="NON_BREAKING",
            )
        )

        assert result.overall_status == "drifted"
        assert len(result.drifted_contracts) == 1
        finding = result.drifted_contracts[0]
        assert finding.severity == "NON_BREAKING"
        assert any(
            change.path
            == "event_bus.declared_topics.onex.evt.omnimarket.native-sample-completed.v1"
            and not change.is_breaking
            for change in finding.field_changes
        )

    def test_request_model_defaults(self) -> None:
        from omnimarket.nodes.node_contract_drift_compute.handlers.handler_contract_drift_compute import (
            ModelContractDriftComputeRequest,
        )

        req = ModelContractDriftComputeRequest()
        assert req.repos == []
        assert req.baseline_path == ""
        assert req.dry_run is False
        assert req.sensitivity == "STANDARD"
        assert req.severity_threshold == "BREAKING"
        assert req.check_boundaries is True

    def test_result_model_defaults(self) -> None:
        from omnimarket.nodes.node_contract_drift_compute.handlers.handler_contract_drift_compute import (
            ModelContractDriftComputeResult,
        )

        result = ModelContractDriftComputeResult()
        assert result.drifted_contracts == []
        assert result.boundary_findings == []
        assert result.staleness_scores == {}
        assert result.violations == []
        assert result.overall_status == "clean"
        assert result.repos_scanned == 0
        assert result.total_contracts_checked == 0
