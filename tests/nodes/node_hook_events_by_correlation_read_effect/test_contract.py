# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The contract binds the route, the handler and the mount, and holds no value."""

from __future__ import annotations

import pytest
import yaml

from omnimarket.nodes.node_hook_events_by_correlation_read_effect.handlers.handler_hook_events_by_correlation import (
    HandlerHookEventsByCorrelation,
)
from omnimarket.nodes.node_hook_events_by_correlation_read_effect.handlers.http_dependencies import (
    CONTRACT_PATH,
    load_http_config,
)
from omnimarket.protocols.protocol_http_tenant_identity import (
    ProtocolHttpTenantIdentity,
)
from tests.nodes.node_hook_events_by_correlation_read_effect.support import FakeIdentity

pytestmark = pytest.mark.unit


def test_contract_binds_the_handler_and_the_mount_factory():
    contract = yaml.safe_load(CONTRACT_PATH.read_text())
    config = load_http_config()
    assert (config.handler.module, config.handler.name) == (
        HandlerHookEventsByCorrelation.__module__,
        HandlerHookEventsByCorrelation.__name__,
    )
    assert config.methods == ("GET",)
    assert contract["http_mount"]["factory"].endswith("handlers.http_app:create_app")
    handler_entry = contract["handler_routing"]["handlers"][0]["handler"]
    assert handler_entry["name"] == "HandlerHookEventsByCorrelation"
    assert "event_bus" not in contract  # a read route owns no topic


def test_the_contract_names_no_environment_variable_and_no_secret():
    text = CONTRACT_PATH.read_text()
    for forbidden in ("environment:", "os.environ", "_URL", "SECRET", "PASSWORD"):
        assert forbidden not in text


def test_the_handler_package_reads_no_environment():
    root = CONTRACT_PATH.parent
    for path in (root / "handlers").glob("*.py"):
        assert "os.environ" not in path.read_text(), path.name
        assert "getenv" not in path.read_text(), path.name


def test_contract_says_the_tenant_key_is_the_slug():
    description = yaml.safe_load(CONTRACT_PATH.read_text())["description"]
    assert "tenant SLUG" in description
    assert "tenant UUID" in description


def test_the_test_fake_satisfies_the_shared_identity_protocol():
    assert isinstance(FakeIdentity(), ProtocolHttpTenantIdentity)


def test_database_adapter_transport_is_declared_in_the_contract():
    from omnimarket.nodes.node_compliance_sweep.handlers.handler_compliance_sweep import (
        NodeComplianceSweep,
    )

    adapter = CONTRACT_PATH.parent / "handlers" / "http_dependencies.py"
    violations = NodeComplianceSweep()._check_transport_imports(
        "omnimarket", adapter.name, CONTRACT_PATH.parent.name, adapter
    )
    assert violations == []
