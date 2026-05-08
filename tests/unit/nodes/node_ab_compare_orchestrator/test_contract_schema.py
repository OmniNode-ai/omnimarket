# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

NODE_DIR = (
    Path(__file__).resolve().parents[4]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_ab_compare_orchestrator"
)
CONTRACT_PATH = NODE_DIR / "contract.yaml"
REGISTRY_PATH = NODE_DIR / "models_registry.yaml"
WORKFLOW_PATH = Path(__file__).resolve().parents[4] / "ab_compare_workflow.yaml"

_REQUIRED_MODEL_FIELDS = {
    "id",
    "display_name",
    "protocol",
    "cost_per_1k_input",
    "cost_per_1k_output",
    "location",
    "context_window",
}
_VALID_PROTOCOLS = {"openai_compatible"}
_VALID_LOCATIONS = {"local", "cloud"}


@pytest.mark.unit
def test_contract_yaml_is_well_formed() -> None:
    data = yaml.safe_load(CONTRACT_PATH.read_text())
    assert data["name"] == "ab_compare_orchestrator"
    assert data["node_type"] == "orchestrator"
    assert isinstance(data["contract_version"], dict)
    assert data["contract_version"]["major"] == 1


@pytest.mark.unit
def test_contract_declares_expected_topics() -> None:
    data = yaml.safe_load(CONTRACT_PATH.read_text())
    bus = data["event_bus"]

    assert "onex.cmd.omnimarket.ab-compare-start.v1" in bus["subscribe_topics"]
    assert "onex.cmd.omnimarket.ab-inference-requested.v1" in bus["publish_topics"]
    assert "onex.evt.omnimarket.ab-compare-completed.v1" in bus["publish_topics"]


@pytest.mark.unit
def test_contract_handler_routing_is_present() -> None:
    data = yaml.safe_load(CONTRACT_PATH.read_text())
    routing = data["handler_routing"]
    handlers = routing["handlers"]
    assert len(handlers) >= 1

    entry = handlers[0]
    assert entry["handler"]["name"] == "HandlerAbCompareOrchestrator"
    assert entry["handler"]["module"].startswith(
        "omnimarket.nodes.node_ab_compare_orchestrator"
    )
    assert entry["event_model"]["name"] == "ModelAbCompareStart"


@pytest.mark.unit
def test_contract_terminal_event_matches_publish_topic() -> None:
    data = yaml.safe_load(CONTRACT_PATH.read_text())
    terminal = data["terminal_event"]
    published = data["event_bus"]["publish_topics"]
    assert terminal in published, (
        f"terminal_event '{terminal}' must appear in event_bus.publish_topics"
    )


@pytest.mark.unit
def test_models_registry_schema_version() -> None:
    data = yaml.safe_load(REGISTRY_PATH.read_text())
    assert data["schema_version"] == "1.0.0"
    assert isinstance(data["models"], list)
    assert len(data["models"]) >= 1


@pytest.mark.unit
def test_models_registry_all_models_have_required_fields() -> None:
    data = yaml.safe_load(REGISTRY_PATH.read_text())
    for model in data["models"]:
        missing = _REQUIRED_MODEL_FIELDS - set(model.keys())
        assert not missing, f"Model '{model.get('id')}' missing fields: {missing}"


@pytest.mark.unit
def test_models_registry_protocols_are_valid() -> None:
    data = yaml.safe_load(REGISTRY_PATH.read_text())
    for model in data["models"]:
        assert model["protocol"] in _VALID_PROTOCOLS, (
            f"Model '{model['id']}' has invalid protocol '{model['protocol']}'"
        )


@pytest.mark.unit
def test_models_registry_locations_are_valid() -> None:
    data = yaml.safe_load(REGISTRY_PATH.read_text())
    for model in data["models"]:
        assert model["location"] in _VALID_LOCATIONS, (
            f"Model '{model['id']}' has invalid location '{model['location']}'"
        )


@pytest.mark.unit
def test_models_registry_openai_models_have_base_endpoint() -> None:
    """Every openai_compatible model must declare either `endpoint` (literal
    base URL — typical for cloud) or `endpoint_env` (env var lookup — required
    for local lab models per OMN-10645 to avoid baking lab IPs in the registry).
    """
    data = yaml.safe_load(REGISTRY_PATH.read_text())
    for model in data["models"]:
        if model["protocol"] == "openai_compatible":
            has_literal = "endpoint" in model
            has_env = "endpoint_env" in model and bool(model["endpoint_env"])
            assert has_literal or has_env, (
                f"OpenAI-compatible model '{model['id']}' must declare "
                "either `endpoint` or `endpoint_env`"
            )
            if has_literal:
                assert "/v1/chat/completions" not in model["endpoint"], (
                    f"OpenAI-compatible model '{model['id']}' must declare a base "
                    "endpoint; the effect node appends the chat completions path"
                )


@pytest.mark.unit
def test_models_registry_cloud_models_require_key() -> None:
    data = yaml.safe_load(REGISTRY_PATH.read_text())
    for model in data["models"]:
        if model["location"] == "cloud":
            assert "requires_key" in model, (
                f"Cloud model '{model['id']}' must declare requires_key"
            )


@pytest.mark.unit
def test_models_registry_costs_are_non_negative() -> None:
    data = yaml.safe_load(REGISTRY_PATH.read_text())
    for model in data["models"]:
        assert model["cost_per_1k_input"] >= 0, (
            f"Model '{model['id']}' has negative cost_per_1k_input"
        )
        assert model["cost_per_1k_output"] >= 0, (
            f"Model '{model['id']}' has negative cost_per_1k_output"
        )


@pytest.mark.unit
def test_models_registry_local_models_have_zero_cost() -> None:
    data = yaml.safe_load(REGISTRY_PATH.read_text())
    for model in data["models"]:
        if model["location"] == "local":
            assert model["cost_per_1k_input"] == 0.0, (
                f"Local model '{model['id']}' must have zero cost_per_1k_input"
            )
            assert model["cost_per_1k_output"] == 0.0, (
                f"Local model '{model['id']}' must have zero cost_per_1k_output"
            )


@pytest.mark.unit
def test_models_registry_ids_are_unique() -> None:
    data = yaml.safe_load(REGISTRY_PATH.read_text())
    ids = [m["id"] for m in data["models"]]
    assert len(ids) == len(set(ids)), f"Duplicate model IDs found: {ids}"


@pytest.mark.unit
def test_models_registry_has_expected_models() -> None:
    data = yaml.safe_load(REGISTRY_PATH.read_text())
    ids = {m["id"] for m in data["models"]}
    expected = {
        "qwen3-coder-30b",
        "deepseek-r1-14b",
        "deepseek-r1-32b",
        "qwen3-next-80b",
        "glm-4.5",
    }
    assert expected == ids


@pytest.mark.unit
def test_workflow_yaml_is_well_formed() -> None:
    data = yaml.safe_load(WORKFLOW_PATH.read_text())
    assert data["name"] == "ab_compare_workflow"
    assert data["node_type"] == "orchestrator"
    assert data["terminal_event"] == "onex.evt.omnimarket.ab-compare-completed.v1"


@pytest.mark.unit
def test_workflow_yaml_handler_references_orchestrator_module() -> None:
    data = yaml.safe_load(WORKFLOW_PATH.read_text())
    handler = data["handler"]
    assert handler["module"].startswith("omnimarket.nodes.node_ab_compare_orchestrator")
    assert handler["class"] == "HandlerAbCompareOrchestrator"
    input_model = handler["input_model"]
    assert input_model["module"].startswith(
        "omnimarket.nodes.node_ab_compare_orchestrator"
    )
    assert input_model["class"] == "ModelAbCompareStart"
