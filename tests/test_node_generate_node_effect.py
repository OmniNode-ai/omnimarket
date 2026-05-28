"""Tests for node_generate_node_effect (OMN-12230)."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
import yaml

from omnimarket.nodes.node_generate_node_effect.handlers.handler_generate_node import (
    HandlerGenerateNode,
)
from omnimarket.nodes.node_generate_node_effect.models.model_generate_node_command import (
    EnumNodeType,
    ModelGenerateNodeCommand,
)
from omnimarket.nodes.node_generate_node_effect.models.model_generate_node_result import (
    ModelGenerateNodeResult,
)

CONTRACT_PATH = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_generate_node_effect"
    / "contract.yaml"
)


# ---------------------------------------------------------------------------
# Contract shape
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_contract_node_type_is_effect_generic() -> None:
    data = yaml.safe_load(CONTRACT_PATH.read_text())
    assert data["node_type"] == "EFFECT_GENERIC"


@pytest.mark.unit
def test_contract_node_is_implemented() -> None:
    data = yaml.safe_load(CONTRACT_PATH.read_text())
    assert data.get("node_not_implemented") is False


@pytest.mark.unit
def test_contract_declares_kafka_topics() -> None:
    data = yaml.safe_load(CONTRACT_PATH.read_text())
    event_bus = data["event_bus"]
    assert event_bus["subscribe_topics"] == [
        "onex.cmd.omnimarket.generate-node-start.v1"
    ]
    assert event_bus["publish_topics"] == [
        "onex.evt.omnimarket.generate-node-completed.v1"
    ]


@pytest.mark.unit
def test_contract_declares_filesystem_write_side_effect() -> None:
    data = yaml.safe_load(CONTRACT_PATH.read_text())
    writes = data["side_effects"]["writes"]
    assert "filesystem" in writes


@pytest.mark.unit
def test_contract_input_output_models_declared() -> None:
    data = yaml.safe_load(CONTRACT_PATH.read_text())
    assert data["input_model"]["name"] == "ModelGenerateNodeCommand"
    assert data["output_model"]["name"] == "ModelGenerateNodeResult"


# ---------------------------------------------------------------------------
# Model validation
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_command_model_roundtrip() -> None:
    cmd = ModelGenerateNodeCommand(
        correlation_id=uuid4(),
        node_name="node_my_feature_effect",
        node_type=EnumNodeType.EFFECT,
        output_dir="/tmp/generated",
        template_args={"author": "OmniNode"},
    )
    assert cmd.node_name == "node_my_feature_effect"
    assert cmd.node_type == EnumNodeType.EFFECT
    assert cmd.template_args == {"author": "OmniNode"}
    assert cmd.dry_run is False


@pytest.mark.unit
def test_command_model_rejects_invalid_node_name() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ModelGenerateNodeCommand(
            correlation_id=uuid4(),
            node_name="MyNode",  # must match ^node_[a-z][a-z0-9_]*$
            node_type=EnumNodeType.COMPUTE,
            output_dir="/tmp/generated",
        )


@pytest.mark.unit
def test_result_model_roundtrip() -> None:
    cid = uuid4()
    result = ModelGenerateNodeResult(
        correlation_id=cid,
        node_name="node_my_feature_effect",
        created_files=("contract.yaml", "handlers/handler_my_feature.py"),
        output_dir="/tmp/generated",
    )
    assert result.node_name == "node_my_feature_effect"
    assert len(result.created_files) == 2
    assert result.dry_run is False


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_handler_dry_run_returns_manifest(tmp_path: Path) -> None:
    handler = HandlerGenerateNode()
    cmd = ModelGenerateNodeCommand(
        correlation_id=uuid4(),
        node_name="node_manifest_test_effect",
        node_type=EnumNodeType.EFFECT,
        output_dir=str(tmp_path / "node_manifest_test_effect"),
        dry_run=True,
    )
    result = handler.handle(cmd)

    assert result.dry_run is True
    assert "contract.yaml" in result.created_files
    assert not Path(result.output_dir).exists()


@pytest.mark.unit
def test_handler_writes_generated_files(tmp_path: Path) -> None:
    output_dir = tmp_path / "node_generated_test_compute"
    cmd = ModelGenerateNodeCommand(
        correlation_id=uuid4(),
        node_name="node_generated_test_compute",
        node_type=EnumNodeType.COMPUTE,
        output_dir=str(output_dir),
    )
    result = HandlerGenerateNode().handle(cmd)

    assert result.dry_run is False
    assert (output_dir / "contract.yaml").is_file()
    assert (output_dir / "handlers" / "handler_generated_test_compute.py").is_file()
    assert (output_dir / "models" / "model_generated_test_compute_request.py").is_file()
