# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for ContractRegistryHandler — 7 policy enforcement cases."""

from __future__ import annotations

import hashlib
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import yaml
from omnibase_core.models.contracts.model_handler_contract import (
    ModelHandlerContract,
)
from omnibase_infra.runtime.kafka_contract_source import ContractYamlParser

from omnimarket.nodes.node_contract_registry.handlers.handler_contract_registry import (
    ContractRegistryHandler,
    EventPublisher,
)
from omnimarket.nodes.node_contract_registry.models.enums import (
    EnumMaterializationRejection,
    EnumMaterializationStatus,
)
from omnimarket.nodes.node_contract_registry.models.models import (
    ModelContractRegistrationRequest,
)

_ACTIVE_PROFILES: tuple[str, ...] = ("stability", "demo")
_REAL_CONTRACT_PATH = (
    Path(__file__).resolve().parents[4]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_contract_registry"
    / "contract.yaml"
)

_VALID_CONTRACT_YAML = """\
name: node_example
node_type: EFFECT_GENERIC
contract_version:
  major: 0
  minor: 1
  patch: 0
runtime_profiles:
  - stability
descriptor:
  node_archetype: effect
  purity: impure
  idempotent: false
handler:
  module: omnimarket.nodes.node_example.handlers.handler_example
  class: ExampleHandler
  input_model: omnimarket.nodes.node_example.models.model_example.ModelExampleRequest
handler_routing:
  routing_strategy: payload_type_match
  handlers:
    - handler:
        name: ExampleHandler
        module: omnimarket.nodes.node_example.handlers.handler_example
db_io:
  db_tables:
    - name: node_examples
      database_ref: application
      schema: omninode_internal
      migration: 0001_create_node_examples.sql
      access: write
      role: examples
event_bus:
  version:
    major: 1
    minor: 0
    patch: 0
  subscribe_topics:
    - onex.cmd.omnimarket.node-example.v1
  publish_topics:
    - onex.evt.omnimarket.node-example-completed.v1
  dlq_topics:
    - onex.dlq.omnimarket.node-example.v1
  consumer_group: omnimarket.node-example.consume.v1
  plugin_managed: false
  consumer_purpose: contract-registry-test
  tenant_scoped_ingress: false
  terminal_event: onex.evt.omnimarket.node-example-completed.v1
  publish_topic_metadata:
    onex.evt.omnimarket.node-example-completed.v1:
      description: Producer-only authoring metadata.
"""


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _make_request(**overrides: Any) -> ModelContractRegistrationRequest:
    defaults: dict[str, Any] = {
        "node_name": "node_example",
        "contract_yaml": _VALID_CONTRACT_YAML,
        "contract_hash": _sha256(_VALID_CONTRACT_YAML),
        "correlation_id": uuid.uuid4(),
    }
    defaults.update(overrides)
    return ModelContractRegistrationRequest(**defaults)


def _make_handler(**kwargs: Any) -> ContractRegistryHandler:
    return ContractRegistryHandler(
        active_profiles=_ACTIVE_PROFILES,
        **kwargs,
    )


@pytest.mark.unit
def test_valid_contract_materialized() -> None:
    publisher = MagicMock(spec=EventPublisher)
    handler = _make_handler(publisher=publisher)
    request = _make_request(
        node_version={"major": 0, "minor": 1, "patch": 0},
        deployer_id="codex-integration-pass",
        target_profile="stability",
    )

    result = handler.handle(request)

    assert result.status == EnumMaterializationStatus.MATERIALIZED
    assert result.stored is True
    assert result.published_topic == "onex.evt.platform.node-registration.v1"
    publisher.publish.assert_called_once()
    topic, payload = publisher.publish.call_args[0]
    assert topic == "onex.evt.platform.node-registration.v1"
    assert payload["node_name"] == "node_example"
    assert payload["event_type"] == "registered"
    # OMN-12463: the published contract_yaml is the ModelHandlerContract-shaped
    # payload (adapted producer-side), not the raw market contract.
    published_contract = yaml.safe_load(payload["contract_yaml"])
    assert published_contract["handler_id"] == "node.node_example"
    assert (
        published_contract["metadata"]["handler_class"]
        == "omnimarket.nodes.node_example.handlers.handler_example.ExampleHandler"
    )
    ModelHandlerContract.model_validate(
        {
            key: value
            for key, value in published_contract.items()
            if key not in {"db_io", "event_bus", "handler_routing"}
        }
    )
    descriptor = ContractYamlParser(environment="test").parse(
        "node_example",
        payload["contract_yaml"],
        request.correlation_id,
    )
    assert descriptor.contract_config["db_io"] == {
        "db_tables": [
            {
                "name": "node_examples",
                "database_ref": "application",
                "schema": "omninode_internal",
                "migration": "0001_create_node_examples.sql",
                "access": "write",
                "role": "examples",
            }
        ]
    }
    assert (
        descriptor.contract_config["handler_routing"]
        == yaml.safe_load(_VALID_CONTRACT_YAML)["handler_routing"]
    )
    assert descriptor.contract_config["event_bus"] == {
        "subscribe_topics": ["onex.cmd.omnimarket.node-example.v1"],
        "publish_topics": ["onex.evt.omnimarket.node-example-completed.v1"],
        "dlq_topics": ["onex.dlq.omnimarket.node-example.v1"],
        "consumer_group": "omnimarket.node-example.consume.v1",
        "plugin_managed": False,
        "consumer_purpose": "contract-registry-test",
        "tenant_scoped_ingress": False,
        "terminal_event": "onex.evt.omnimarket.node-example-completed.v1",
    }
    assert payload["contract_hash"] == _sha256(_VALID_CONTRACT_YAML)
    assert payload["correlation_id"] == str(request.correlation_id)
    assert payload["node_version"] == {"major": 0, "minor": 1, "patch": 0}
    assert payload["deployer_id"] == "codex-integration-pass"
    assert payload["target_profile"] == "stability"


@pytest.mark.unit
def test_real_registry_contract_publishes_parser_consumable_runtime_shape() -> None:
    contract_yaml = _REAL_CONTRACT_PATH.read_text(encoding="utf-8")
    publisher = MagicMock(spec=EventPublisher)
    handler = _make_handler(publisher=publisher)
    request = _make_request(
        node_name="node_contract_registry",
        contract_yaml=contract_yaml,
        contract_hash=_sha256(contract_yaml),
    )

    result = handler.handle(request)

    assert result.status == EnumMaterializationStatus.MATERIALIZED
    _, published = publisher.publish.call_args[0]
    descriptor = ContractYamlParser(environment="test").parse(
        "node_contract_registry",
        published["contract_yaml"],
        request.correlation_id,
    )
    config = descriptor.contract_config
    assert config["db_io"]["db_tables"] == [
        {
            "name": "contract_registry",
            "database_ref": "application",
            "schema": "omninode_internal",
            "migration": "0000_create_contract_registry.sql",
            "access": "write",
            "role": "contract_registry",
        }
    ]
    assert config["handler_routing"]["routing_strategy"] == "payload_type_match"
    assert config["event_bus"] == {
        "subscribe_topics": ["onex.cmd.platform.node-registration-requested.v1"],
        "publish_topics": [
            "onex.evt.platform.node-registration.v1",
            "onex.evt.platform.node-registration-rejected.v1",
        ],
    }


@pytest.mark.unit
def test_hash_mismatch_rejected() -> None:
    publisher = MagicMock(spec=EventPublisher)
    handler = _make_handler(publisher=publisher)
    request = _make_request(contract_hash="deadbeef" * 8)

    result = handler.handle(request)

    assert result.status == EnumMaterializationStatus.REJECTED
    assert result.reason == EnumMaterializationRejection.HASH_MISMATCH
    assert result.stored is False
    publisher.publish.assert_called_once()
    topic, _ = publisher.publish.call_args[0]
    assert topic == "onex.evt.platform.node-registration-rejected.v1"


@pytest.mark.unit
def test_canonical_command_fields_allowed_and_forwarded() -> None:
    publisher = MagicMock(spec=EventPublisher)
    handler = _make_handler(publisher=publisher)
    registration_request_id = uuid.uuid4()
    request = _make_request(
        registration_request_id=registration_request_id,
        runtime_profiles=("stability",),
        source_repo="onex-self-extending-agent",
        source_commit_sha="abc123",
        generated_by="codex",
        deployment_identity="stability-validator",
        source_environment="stability-test",
        generated_by_agent="codex",
        trusted_artifact_ref="local-worktree",
        target_profile="stability-test",
    )

    result = handler.handle(request)

    assert result.status == EnumMaterializationStatus.MATERIALIZED
    publisher.publish.assert_called_once()
    topic, payload = publisher.publish.call_args[0]
    assert topic == "onex.evt.platform.node-registration.v1"
    assert payload["registration_request_id"] == str(registration_request_id)
    assert payload["runtime_profile"] == "stability-test"
    assert payload["materialization_result"] == "materialized"


@pytest.mark.unit
def test_handler_allowlist_rejected() -> None:
    bad_yaml = """\
name: node_malicious
node_type: EFFECT_GENERIC
runtime_profiles:
  - stability
handler_routing:
  routing_strategy: payload_type_match
  handlers:
    - handler:
        name: BadHandler
        module: os.system
"""
    publisher = MagicMock(spec=EventPublisher)
    handler = _make_handler(publisher=publisher)
    request = _make_request(
        node_name="node_malicious",
        contract_yaml=bad_yaml,
        contract_hash=_sha256(bad_yaml),
    )

    result = handler.handle(request)

    assert result.status == EnumMaterializationStatus.REJECTED
    assert result.reason == EnumMaterializationRejection.HANDLER_ALLOWLIST


@pytest.mark.unit
def test_missing_runtime_profiles_rejected() -> None:
    no_profile_yaml = """\
name: node_no_profile
node_type: EFFECT_GENERIC
runtime_profiles:
  - prod_only
handler_routing:
  routing_strategy: payload_type_match
  handlers:
    - handler:
        name: SomeHandler
        module: omnimarket.nodes.node_no_profile.handlers.handler
"""
    publisher = MagicMock(spec=EventPublisher)
    handler = _make_handler(publisher=publisher)
    request = _make_request(
        node_name="node_no_profile",
        contract_yaml=no_profile_yaml,
        contract_hash=_sha256(no_profile_yaml),
    )

    result = handler.handle(request)

    assert result.status == EnumMaterializationStatus.REJECTED
    assert result.reason == EnumMaterializationRejection.PROFILE_MISMATCH


@pytest.mark.unit
def test_version_conflict_rejected() -> None:
    handler = _make_handler()
    request_v1 = _make_request()
    handler.handle(request_v1)

    different_yaml = _VALID_CONTRACT_YAML + "# extra\n"
    request_v2 = _make_request(
        contract_yaml=different_yaml,
        contract_hash=_sha256(different_yaml),
    )

    result = handler.handle(request_v2)

    assert result.status == EnumMaterializationStatus.REJECTED
    assert result.reason == EnumMaterializationRejection.VERSION_CONFLICT


@pytest.mark.unit
def test_same_name_same_hash_idempotent() -> None:
    handler = _make_handler()
    request = _make_request()

    result_first = handler.handle(request)
    result_second = handler.handle(request)

    assert result_first.status == EnumMaterializationStatus.MATERIALIZED
    assert result_second.status == EnumMaterializationStatus.ALREADY_MATERIALIZED
    assert result_second.stored is True


@pytest.mark.unit
def test_unadaptable_contract_rejected() -> None:
    # Passes allowlist + profile checks but declares no derivable input model,
    # so the producer-side adapter (OMN-12463) fails fast and the registration
    # is rejected rather than published with a malformed payload.
    no_input_yaml = """\
name: node_no_input
node_type: EFFECT_GENERIC
contract_version:
  major: 1
  minor: 0
  patch: 0
runtime_profiles:
  - stability
descriptor:
  node_archetype: effect
handler:
  module: omnimarket.nodes.node_no_input.handlers.handler_no_input
  class: HandlerNoInput
"""
    publisher = MagicMock(spec=EventPublisher)
    handler = _make_handler(publisher=publisher)
    request = _make_request(
        node_name="node_no_input",
        contract_yaml=no_input_yaml,
        contract_hash=_sha256(no_input_yaml),
    )

    result = handler.handle(request)

    assert result.status == EnumMaterializationStatus.REJECTED
    assert result.reason == EnumMaterializationRejection.ADAPTER_FAILURE
    topic, _ = publisher.publish.call_args[0]
    assert topic == "onex.evt.platform.node-registration-rejected.v1"


@pytest.mark.unit
def test_malformed_yaml_rejected() -> None:
    bad_yaml = "key: [unclosed bracket\n  - item"
    publisher = MagicMock(spec=EventPublisher)
    handler = _make_handler(publisher=publisher)
    request = _make_request(
        node_name="node_broken",
        contract_yaml=bad_yaml,
        contract_hash=_sha256(bad_yaml),
    )

    result = handler.handle(request)

    assert result.status == EnumMaterializationStatus.REJECTED
    assert result.reason == EnumMaterializationRejection.PARSE_FAILURE
    publisher.publish.assert_called_once()
    topic, _ = publisher.publish.call_args[0]
    assert topic == "onex.evt.platform.node-registration-rejected.v1"
