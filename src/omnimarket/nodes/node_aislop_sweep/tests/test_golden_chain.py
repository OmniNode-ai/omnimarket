# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Golden chain test for node_aislop_sweep — zero infra, EventBusInmemory.

Verifies OMN-7537: the aislop_sweep node can be loaded and executed
standalone with no external infrastructure (no Kafka, no Postgres).

This is the canonical golden chain pattern:
1. Contract YAML is valid and loadable
2. Handler can be instantiated and called
3. Node produces valid output with zero infra
"""

from __future__ import annotations

from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def node_dir() -> Path:
    """Path to the node_aislop_sweep directory."""
    return Path(__file__).resolve().parent.parent


@pytest.fixture
def contract_path(node_dir: Path) -> Path:
    """Path to the node's contract.yaml."""
    return node_dir / "contract.yaml"


@pytest.fixture
def metadata_path(node_dir: Path) -> Path:
    """Path to the node's metadata.yaml."""
    return node_dir / "metadata.yaml"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestContractYaml:
    """Contract YAML is valid and has required fields."""

    def test_contract_exists(self, contract_path: Path) -> None:
        assert contract_path.exists(), f"contract.yaml not found at {contract_path}"

    def test_contract_loads(self, contract_path: Path) -> None:
        import yaml

        with open(contract_path) as f:
            data = yaml.safe_load(f)
        assert isinstance(data, dict)
        assert "name" in data
        assert "contract_version" in data or "version" in data
        assert "handler" in data

    def test_contract_declares_handler(self, contract_path: Path) -> None:
        import yaml

        with open(contract_path) as f:
            data = yaml.safe_load(f)
        handler = data.get("handler", {})
        assert "module" in handler, "handler.module not declared"
        assert "class" in handler, "handler.class not declared"


class TestMetadataYaml:
    """Metadata YAML is valid and has required fields."""

    def test_metadata_exists(self, metadata_path: Path) -> None:
        assert metadata_path.exists(), f"metadata.yaml not found at {metadata_path}"

    def test_metadata_loads(self, metadata_path: Path) -> None:
        import yaml

        with open(metadata_path) as f:
            data = yaml.safe_load(f)
        assert isinstance(data, dict)
        assert "name" in data
        assert "version" in data
        assert "entry_points" in data


class TestHandlerImport:
    """Handler module can be imported and class instantiated."""

    def test_handler_module_imports(self) -> None:
        from omnimarket.nodes.node_aislop_sweep.handlers import (
            handler_aislop_sweep,
        )

        assert handler_aislop_sweep is not None

    def test_handler_class_exists(self) -> None:
        from omnimarket.nodes.node_aislop_sweep.handlers.handler_aislop_sweep import (
            NodeAislopSweep,
        )

        assert NodeAislopSweep is not None

    def test_input_model_exists(self) -> None:
        from omnimarket.nodes.node_aislop_sweep.handlers.handler_aislop_sweep import (
            AislopSweepRequest,
        )

        assert AislopSweepRequest is not None


class TestHandlerExecution:
    """Handler executes with zero infra and produces valid output."""

    def test_handler_runs_with_empty_repos(self) -> None:
        """Handler returns empty findings when given no repos to scan."""
        from omnimarket.nodes.node_aislop_sweep.handlers.handler_aislop_sweep import (
            AislopSweepRequest,
            NodeAislopSweep,
        )

        handler = NodeAislopSweep()
        request = AislopSweepRequest(target_dirs=[], dry_run=True)

        result = handler.handle(request)

        # Result should have findings list and status
        assert result is not None
        assert hasattr(result, "findings")
        assert hasattr(result, "status")

    def test_handler_scans_real_node(self, node_dir: Path) -> None:
        """Handler scans its own source directory without crashing."""
        from omnimarket.nodes.node_aislop_sweep.handlers.handler_aislop_sweep import (
            AislopSweepRequest,
            NodeAislopSweep,
        )

        handler = NodeAislopSweep()
        # Scan the node's own source — should find some patterns or return empty
        request = AislopSweepRequest(
            target_dirs=[str(node_dir)],  # node_aislop_sweep/
            dry_run=True,
        )

        result = handler.handle(request)

        # Should complete without raising
        assert result is not None
