# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain test for node_checkpoint_compute — zero infra.

Verifies OMN-12226: contract YAML is valid, handler is importable,
and the stub raises NotImplementedError as declared.
"""

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
        from omnimarket.nodes.node_checkpoint_compute.handlers import (  # noqa: F401
            handler_checkpoint_compute,
        )

    def test_handler_class_exists(self) -> None:
        from omnimarket.nodes.node_checkpoint_compute.handlers.handler_checkpoint_compute import (
            HandlerCheckpointCompute,
        )

        assert HandlerCheckpointCompute is not None

    def test_input_model_exists(self) -> None:
        from omnimarket.nodes.node_checkpoint_compute.models.model_checkpoint_request import (
            ModelCheckpointRequest,
        )

        assert ModelCheckpointRequest is not None

    def test_output_model_exists(self) -> None:
        from omnimarket.nodes.node_checkpoint_compute.models.model_checkpoint_result import (
            ModelCheckpointResult,
        )

        assert ModelCheckpointResult is not None


class TestHandlerStub:
    def test_handler_raises_not_implemented(self) -> None:
        from omnimarket.nodes.node_checkpoint_compute.handlers.handler_checkpoint_compute import (
            HandlerCheckpointCompute,
        )
        from omnimarket.nodes.node_checkpoint_compute.models.model_checkpoint_request import (
            ModelCheckpointRequest,
        )

        handler = HandlerCheckpointCompute()
        request = ModelCheckpointRequest(checkpoint_id="test-001", action="load")
        with pytest.raises(NotImplementedError):
            handler.handle(request)
