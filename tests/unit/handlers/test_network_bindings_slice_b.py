# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Four modules read their loopback names and the standalone API port from the authority (OMN-21692)."""

from __future__ import annotations

from pathlib import Path

import pytest
from omnibase_core.validation.validator_contracts_and_di import ValidatorContractsAndDi

from omnimarket.handlers import customer_key_terminus
from omnimarket.nodes.node_captured_secret_store_effect.handlers import (
    handler_captured_secret_store,
)
from omnimarket.nodes.node_local_dashboard_serve_effect.models import (
    model_local_dashboard,
)
from omnimarket.nodes.node_ticket_text_effect.handlers.handler_ticket_text import (
    linear_url_ok,
)

SLICE = (
    "nodes/node_captured_secret_store_effect/handlers/handler_captured_secret_store.py",
    "nodes/node_local_dashboard_serve_effect/models/model_local_dashboard.py",
    "nodes/node_ticket_text_effect/handlers/handler_ticket_text.py",
    "handlers/customer_key_terminus.py",
)


@pytest.mark.unit
def test_slice_has_no_host_ip_or_port_findings() -> None:
    root = Path(__file__).resolve().parents[3] / "src" / "omnimarket"
    paths = [root / relative for relative in SLICE]
    rules = {"literal_host", "literal_ip", "literal_port"}
    assert all(path.is_file() for path in paths), "a slice module is missing"
    validator = ValidatorContractsAndDi()
    enabled = {rule.rule_id for rule in validator.contract.rules if rule.enabled}
    assert rules <= enabled, f"network rules are not enabled: {sorted(rules - enabled)}"
    result = validator.validate(paths)
    assert result.metadata is not None
    assert result.metadata.files_processed == len(paths)
    offenders = [str(i) for i in result.issues if i.rule_name in rules]
    assert not offenders, offenders


@pytest.mark.unit
def test_dashboard_keeps_its_previous_bind_values() -> None:
    assert model_local_dashboard._STANDALONE_API_PORT == 3002
    assert model_local_dashboard._DEFAULT_HOST == "127.0.0.1"
    assert frozenset({"localhost"}) == model_local_dashboard._LOOPBACK_NAMES
    assert model_local_dashboard.is_loopback_host("localhost")
    assert model_local_dashboard.is_loopback_host("127.0.0.1")
    assert not model_local_dashboard.is_loopback_host("192.0.2.1")


@pytest.mark.unit
def test_loopback_name_checks_are_unchanged() -> None:
    assert linear_url_ok("http://localhost:8080/graphql")
    assert linear_url_ok("https://api.linear.app/graphql")
    assert not linear_url_ok("http://example.com/graphql")
    assert handler_captured_secret_store._plain_http_allowed("localhost")
    assert handler_captured_secret_store._plain_http_allowed("127.0.0.1")
    assert not handler_captured_secret_store._plain_http_allowed("example.com")
    assert not handler_captured_secret_store._plain_http_allowed(None)
    assert customer_key_terminus.is_non_public_host("localhost")
    assert customer_key_terminus.is_non_public_host("printer.local")
    assert not customer_key_terminus.is_non_public_host("example.com")
