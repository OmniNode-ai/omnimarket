# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain test for node_env_sync_alert_effect — zero infra.

Verifies OMN-12227: contract YAML is valid, handler is importable,
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
        from omnimarket.nodes.node_env_sync_alert_effect.handlers import (  # noqa: F401
            handler_env_sync_alert_effect,
        )

    def test_handler_class_exists(self) -> None:
        from omnimarket.nodes.node_env_sync_alert_effect.handlers.handler_env_sync_alert_effect import (
            HandlerEnvSyncAlertEffect,
        )

        assert HandlerEnvSyncAlertEffect is not None

    def test_input_model_exists(self) -> None:
        from omnimarket.nodes.node_env_sync_alert_effect.models.model_env_sync_alert_request import (
            ModelEnvSyncAlertRequest,
        )

        assert ModelEnvSyncAlertRequest is not None

    def test_output_model_exists(self) -> None:
        from omnimarket.nodes.node_env_sync_alert_effect.models.model_env_sync_alert_result import (
            ModelEnvSyncAlertResult,
        )

        assert ModelEnvSyncAlertResult is not None


class TestHandlerStub:
    def test_handler_raises_not_implemented(self) -> None:
        from omnimarket.nodes.node_env_sync_alert_effect.handlers.handler_env_sync_alert_effect import (
            HandlerEnvSyncAlertEffect,
        )
        from omnimarket.nodes.node_env_sync_alert_effect.models.model_env_sync_alert_request import (
            ModelEnvSyncAlertRequest,
        )

        handler = HandlerEnvSyncAlertEffect()
        request = ModelEnvSyncAlertRequest(log_paths=["/var/log/onex.log"])
        with pytest.raises(NotImplementedError):
            handler.handle(request)
