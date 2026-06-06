# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain test for node_env_sync_alert_effect — zero infra."""

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


class TestHandler:
    def test_handler_emits_friction_event(self, tmp_path: Path) -> None:
        from omnimarket.nodes.node_env_sync_alert_effect.handlers.handler_env_sync_alert_effect import (
            HandlerEnvSyncAlertEffect,
        )
        from omnimarket.nodes.node_env_sync_alert_effect.models.model_env_sync_alert_request import (
            ModelEnvSyncAlertRequest,
        )

        log_path = tmp_path / "runtime.log"
        log_path.write_text(
            "\n".join(
                [
                    "ok startup",
                    "ENV_SYNC_DRIFT missing DATABASE_URL on stability runtime",
                    "environment sync drift: DATABASE_URL differs from contract",
                ]
            ),
            encoding="utf-8",
        )
        friction_dir = tmp_path / "friction"
        handler = HandlerEnvSyncAlertEffect()
        request = ModelEnvSyncAlertRequest(
            log_paths=[str(log_path)],
            alert_threshold=1,
            friction_dir=str(friction_dir),
        )
        result = handler.handle(request)

        assert result.alerts_created == 0
        assert len(result.friction_events) == 1
        assert result.friction_events[0]["env_keys"] == ["DATABASE_URL"]
        assert Path(result.friction_events[0]["friction_path"]).is_file()

    def test_handler_creates_linear_tickets_through_adapter(
        self, tmp_path: Path
    ) -> None:
        from omnimarket.nodes.node_env_sync_alert_effect.handlers.handler_env_sync_alert_effect import (
            HandlerEnvSyncAlertEffect,
        )
        from omnimarket.nodes.node_env_sync_alert_effect.models.model_env_sync_alert_request import (
            ModelEnvSyncAlertRequest,
        )

        class Adapter:
            def __init__(self) -> None:
                self.payloads: list[dict[str, object]] = []

            def create_ticket(self, payload: dict[str, object]) -> str:
                self.payloads.append(payload)
                return "OMN-1"

        log_path = tmp_path / "runtime.log"
        log_path.write_text("env sync drift: REDPANDA_URL missing\n", encoding="utf-8")
        adapter = Adapter()
        result = HandlerEnvSyncAlertEffect(linear_adapter=adapter).handle(
            ModelEnvSyncAlertRequest(
                log_paths=[str(log_path)],
                friction_dir=str(tmp_path / "friction"),
                create_linear_tickets=True,
            )
        )

        assert result.alerts_created == 1
        assert adapter.payloads[0]["drift_signature"]
