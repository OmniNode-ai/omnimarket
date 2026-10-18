# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20993: catalog rows -> HandlerKeyPoolExpiryAlert -> one slack-publish command."""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest
import yaml

import omnimarket.nodes.node_key_pool_expiry_alert_compute as node_package
from omnimarket.nodes.node_key_pool_expiry_alert_compute.handlers.handler_key_pool_expiry_alert import (
    HandlerKeyPoolExpiryAlert,
)
from omnimarket.nodes.node_key_pool_expiry_alert_compute.models import (
    ModelKeyPoolExpiryAlertRequest,
    ModelKeyPoolExpiryAlertResult,
)
from omnimarket.nodes.node_slack_publish_effect.models.model_slack_publish import (
    ModelSlackPublish,
)

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3]
NODE_DIR = Path(node_package.__file__).parent
TERMINAL_TOPIC = "onex.evt.omnimarket.key-pool-expiry-alert-evaluated.v1"

REQUEST_JSON = json.dumps(
    {
        "tenant_id": "tenant-acme",
        "channel": "C0KEYPOOL",
        "correlation_id": "00000000-0000-4000-8000-000000000093",
        "now": "2026-12-01T12:00:00Z",
        "credentials": [
            {
                "api_key_ref": "ref-a",
                "tenant_id": "tenant-acme",
                "name": "alpha",
                "fingerprint": "fp-should-never-surface",
                "expires_at": "2026-12-04T12:00:00Z",
            },
            {
                "api_key_ref": "ref-b",
                "tenant_id": "tenant-acme",
                "name": "bravo",
                "expires_at": "2026-12-20T00:00:00Z",
            },
            {
                "api_key_ref": "ref-far",
                "tenant_id": "tenant-acme",
                "name": "far",
                "expires_at": "2027-06-01T00:00:00Z",
            },
        ],
    }
)


def test_contract_binds_the_models_and_the_handler_and_the_entry_point() -> None:
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text())
    assert contract["node_type"] == "compute"
    assert contract["descriptor"]["purity"] == "pure"
    handler_binding = contract["handler"]
    handler_type = getattr(
        importlib.import_module(handler_binding["module"]), handler_binding["class"]
    )
    assert handler_type is HandlerKeyPoolExpiryAlert
    assert issubclass(node_package.NodeKeyPoolExpiryAlertCompute, handler_type)
    input_model = getattr(
        importlib.import_module(contract["input_model"]["module"]),
        contract["input_model"]["name"],
    )
    assert input_model is ModelKeyPoolExpiryAlertRequest
    assert contract["terminal_event"] == TERMINAL_TOPIC
    assert contract["event_bus"]["publish_topics"] == [TERMINAL_TOPIC]
    assert contract["externally_consumed_topics"] == [TERMINAL_TOPIC]
    assert (
        'node_key_pool_expiry_alert_compute = "omnimarket.nodes.'
        'node_key_pool_expiry_alert_compute"' in (ROOT / "pyproject.toml").read_text()
    )


def test_golden_chain_json_request_to_one_deliverable_slack_command() -> None:
    request = ModelKeyPoolExpiryAlertRequest.model_validate_json(REQUEST_JSON)
    result = node_package.NodeKeyPoolExpiryAlertCompute().handle(request)

    assert result.alert is not None
    assert [k.api_key_ref for k in result.alert.expiring] == ["ref-a", "ref-b"]
    assert "fp-should-never-surface" not in result.model_dump_json()

    wire = ModelKeyPoolExpiryAlertResult.model_validate_json(result.model_dump_json())
    assert wire == result
    command = ModelSlackPublish.model_validate(
        result.alert.slack_command.model_dump(mode="json")
    )
    assert command.channel == "C0KEYPOOL"
    assert command.text == result.alert.slack_command.text


def test_golden_chain_is_deterministic_and_stateless() -> None:
    request = ModelKeyPoolExpiryAlertRequest.model_validate_json(REQUEST_JSON)
    handler = HandlerKeyPoolExpiryAlert()
    assert handler.handle(request) == handler.handle(request)
