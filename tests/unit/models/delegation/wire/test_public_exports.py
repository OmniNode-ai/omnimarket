"""Proof for delegation wire public exports."""

from __future__ import annotations

from importlib import import_module, resources
from pathlib import Path

import pytest
import yaml
from omnibase_core.models.delegation.wire.model_delegation_wire_request import (
    ModelDelegationRequest,
)
from omnibase_core.models.delegation.wire.model_task_delegated_event import (
    TASK_DELEGATED_TOPIC_V1,
)

import omnimarket.models.delegation.wire as wire
from omnimarket.adapters.llm.bifrost.config_loader_bifrost_delegation import (
    load_bifrost_delegation_config,
)
from omnimarket.models.delegation.wire import model_bifrost_delegation_config


@pytest.mark.unit
def test_wire_all_exports_are_bound_attributes() -> None:
    """The package barrel must not advertise missing contract symbols."""
    missing = [name for name in wire.__all__ if not hasattr(wire, name)]

    assert missing == []


@pytest.mark.unit
def test_wire_exports_point_to_canonical_models() -> None:
    """Dependents can import stable DTOs from the wire package boundary."""
    assert wire.ModelDelegationRequest is ModelDelegationRequest
    assert wire.TASK_DELEGATED_TOPIC_V1 == TASK_DELEGATED_TOPIC_V1
    assert "extend_task_class" in wire.EnumQualityContractMode.__args__
    assert "replace_task_class" in wire.EnumQualityContractMode.__args__


@pytest.mark.unit
def test_bifrost_public_models_parse_the_loader_contract(tmp_path: Path) -> None:
    """Public config imports and the deployed loader share one DTO family."""
    config_path = (
        Path(__file__).resolve().parents[5]
        / "src/omnimarket/configs/bifrost_delegation.yaml"
    )
    overlay = tmp_path / "empty.yaml"
    overlay.write_text("backends: []\n", encoding="utf-8")
    loaded = load_bifrost_delegation_config(config_path, overlay)
    assert loaded.backends
    assert loaded.routing_rules

    public = wire.ModelBifrostDelegationConfig.model_validate_json(
        loaded.model_dump_json()
    )
    assert type(loaded) is wire.ModelBifrostDelegationConfig
    assert public == loaded
    for name in (
        "ModelBifrostDelegationConfig",
        "ModelDelegationBackendConfig",
        "ModelDelegationCircuitBreakerConfig",
        "ModelDelegationFailoverConfig",
        "ModelDelegationFallbackPolicy",
        "ModelDelegationRoutingRule",
        "ModelDelegationShadowConfig",
    ):
        assert getattr(wire, name) is getattr(model_bifrost_delegation_config, name)
    assert all(
        type(backend) is wire.ModelDelegationBackendConfig
        for backend in loaded.backends
    )
    assert all(
        type(rule) is wire.ModelDelegationRoutingRule for rule in loaded.routing_rules
    )


@pytest.mark.unit
def test_bifrost_public_backend_preserves_loader_fields() -> None:
    """The public backend supports the endpoint, judge tier and output budget."""
    backend = wire.ModelDelegationBackendConfig.model_validate(
        {
            "backend_id": "fixture-judge",
            "endpoint_url_env": "FIXTURE_ENDPOINT_URL",
            "tier": "judge",
            "max_tokens": 1234,
        }
    )
    assert backend.endpoint_url_env == "FIXTURE_ENDPOINT_URL"
    assert backend.tier == "judge"
    assert backend.max_tokens == 1234


@pytest.mark.unit
def test_inference_contract_and_handler_share_request_timeout() -> None:
    """The installed inference contract declares the handler's actual DTO."""
    node_package = "omnibase_infra.nodes.node_llm_inference_effect"
    contract = yaml.safe_load(
        resources.files(node_package)
        .joinpath("contract.yaml")
        .read_text(encoding="utf-8")
    )
    declared = contract["input_model"]
    request = getattr(import_module(declared["module"]), declared["name"])
    handler = import_module(f"{node_package}.handlers.handler_llm_openai_compatible")
    assert handler.ModelLlmInferenceRequest is request
    assert (
        request.model_json_schema()["properties"]["timeout_seconds"]["maximum"]
        == 1800.0
    )
