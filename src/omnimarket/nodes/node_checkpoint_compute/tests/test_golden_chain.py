# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain test for node_checkpoint_compute — zero infra."""

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


class TestHandler:
    def test_handler_save_load_and_list(self, tmp_path: Path) -> None:
        from omnimarket.nodes.node_checkpoint_compute.handlers.handler_checkpoint_compute import (
            HandlerCheckpointCompute,
        )
        from omnimarket.nodes.node_checkpoint_compute.models.model_checkpoint_request import (
            ModelCheckpointRequest,
        )

        handler = HandlerCheckpointCompute(state_dir=tmp_path)
        payload = {
            "task_id": "OMN-12340",
            "agent_id": "agent-001",
            "phase": "executing",
            "progress_pct": 0.5,
        }
        save_result = handler.handle(
            ModelCheckpointRequest(
                checkpoint_id="OMN-12340-agent-001",
                action="save",
                payload=payload,
            )
        )
        load_result = handler.handle(
            ModelCheckpointRequest(checkpoint_id="OMN-12340-agent-001", action="load")
        )
        list_result = handler.handle(
            ModelCheckpointRequest(checkpoint_id="ignored", action="list")
        )

        assert save_result.data is None
        assert load_result.data == payload
        assert list_result.checkpoint_list == ["OMN-12340-agent-001"]
