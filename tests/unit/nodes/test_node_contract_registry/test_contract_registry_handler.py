# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for ContractRegistryHandler — 7 policy enforcement cases."""

from __future__ import annotations

import hashlib
import uuid
from typing import Any
from unittest.mock import MagicMock

import pytest

from omnimarket.nodes.node_contract_registry.handlers.handler_contract_registry import (
    ContractRegistryHandler,
)
from omnimarket.nodes.node_contract_registry.models.enums import (
    EnumMaterializationRejection,
    EnumMaterializationStatus,
)
from omnimarket.nodes.node_contract_registry.models.models import (
    ModelContractRegistrationRequest,
)

_ACTIVE_PROFILES: tuple[str, ...] = ("stability", "demo")

_VALID_CONTRACT_YAML = """\
name: node_example
node_type: EFFECT_GENERIC
contract_version:
  major: 0
  minor: 1
  patch: 0
runtime_profiles:
  - stability
handler_routing:
  routing_strategy: payload_type_match
  handlers:
    - handler:
        name: ExampleHandler
        module: omnimarket.nodes.node_example.handlers.handler_example
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
    publisher = MagicMock()
    handler = _make_handler(publisher=publisher)
    request = _make_request()

    result = handler.handle(request)

    assert result.status == EnumMaterializationStatus.MATERIALIZED
    assert result.stored is True
    assert result.published_topic == "onex.evt.platform.node-registration.v1"
    publisher.publish.assert_called_once()
    topic, payload = publisher.publish.call_args[0]
    assert topic == "onex.evt.platform.node-registration.v1"
    assert payload["node_name"] == "node_example"


@pytest.mark.unit
def test_hash_mismatch_rejected() -> None:
    publisher = MagicMock()
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
    publisher = MagicMock()
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
    publisher = MagicMock()
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
def test_malformed_yaml_rejected() -> None:
    bad_yaml = "key: [unclosed bracket\n  - item"
    publisher = MagicMock()
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
